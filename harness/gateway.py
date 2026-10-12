"""Minimal blast-radius gateway for agent tool calls.

A mock production service plus a governance layer that sits between an
agent and that service. The gateway enforces reversibility tiers,
per-session tool scoping, argument validation, idempotency keys, version
preconditions, per-session mutation and spend budgets, blast-scope
limits, plan-then-apply for gated actions, and an append-only audit log.

The gateway owns session policy and counters. Callers receive an opaque
session ID, not an object they can edit. Approved plans are bound to the
(id, version) pairs they resolved to and to the requesting session,
expire after a TTL, and are consumed only when the call succeeds. Callers
only ever see copies of plans, never the gateway's own records.

A mutation that raises after reaching the service has an unknown
outcome: the effect may have landed before the error. The gateway marks
that idempotency key in-doubt, raises OutcomeUnknown, and refuses to
re-execute the key until an operator records what actually happened
with resolve_in_doubt(). A timeout is not proof that a write failed.
While the outcome is unknown, the call counts against the mutation and
spend budgets as if it landed.

Limits of this mock, by design:
- Identities are plain strings. The approver check only rejects an
  approval under the requester's own name; it cannot stop an agent that
  approves under another name. A real system needs an authenticated
  approver on a channel the agent cannot reach.
- Isolation is by convention. Python cannot stop in-process code from
  reaching the gateway's private state; a real gateway runs as a
  separate service the agent can only call over its API.
- The drift check is check-then-act. A real system must also enforce
  preconditions atomically on the server, because a gateway-side check
  races with concurrent writers.
- Spend is read only from the argument a tool declares in
  TOOL_SPEND_FIELD.
- The blast-scope limit has no approval override: a larger bulk action
  needs a session opened with a higher limit.
- open(), approve(), and resolve_in_doubt() are operator APIs with no
  authentication. In a real deployment the agent gets only a bound call
  capability for one session, never a reference to the gateway itself.
- In-doubt protection is per idempotency key. An agent that re-plans
  with a fresh key is not caught, which is why keys must be derived
  from task intent.

Standard library only.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import time
import copy
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Callable


# ---------------------------------------------------------------------------
# Mock "production" service: side effects, a bulk endpoint, no idempotency
# ---------------------------------------------------------------------------

class MockService:
    def __init__(self) -> None:
        self.resources: dict[str, dict] = {}
        self.refunds: list[dict] = []
        self.side_effects = 0  # every mutation that actually lands

    def seed(self, n: int) -> None:
        for i in range(n):
            rid = f"vm-{i:03d}"
            self.resources[rid] = {"id": rid, "tag": "prod" if i < n // 2 else "idle",
                                   "version": 1}

    def get(self, rid: str) -> dict:
        return dict(self.resources[rid])

    def list(self, tag: str | None = None) -> list[dict]:
        return [dict(r) for r in self.resources.values()
                if tag is None or r["tag"] == tag]

    def retag(self, rid: str, tag: str) -> dict:
        r = self.resources[rid]
        r["tag"], r["version"] = tag, r["version"] + 1
        self.side_effects += 1
        return dict(r)

    def delete(self, rid: str) -> dict:
        self.side_effects += 1
        return self.resources.pop(rid)

    def bulk_delete(self, tag: str) -> list[str]:
        ids = [r["id"] for r in self.resources.values() if r["tag"] == tag]
        for rid in ids:
            self.delete(rid)
        return ids

    def refund(self, order_id: str, cents: int) -> dict:
        rec = {"order_id": order_id, "cents": cents}
        self.refunds.append(rec)
        self.side_effects += 1
        return rec


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------

class Tier(str, Enum):
    READ = "read"                  # no side effects
    REVERSIBLE = "reversible"      # undo is a normal call (retag)
    COMPENSABLE = "compensable"    # undo is a new forward action (refund)
    IRREVERSIBLE = "irreversible"  # no undo (delete, bulk delete)


TOOL_TIERS = {
    "get": Tier.READ,
    "list": Tier.READ,
    "retag": Tier.REVERSIBLE,
    "refund": Tier.COMPENSABLE,
    "delete": Tier.IRREVERSIBLE,
    "bulk_delete": Tier.IRREVERSIBLE,
}

# The argument that carries money for each tool that moves money. Spend
# limits apply only to tools listed here, and those tools must supply it.
TOOL_SPEND_FIELD = {
    "refund": "cents",
}

PLAN_TTL_SECONDS = 300


class Denied(Exception):
    pass


class NeedsApproval(Exception):
    """Carries a read-only copy of the plan. Editing it changes nothing."""
    def __init__(self, plan: dict) -> None:
        super().__init__(f"approval required: {plan['tool']}")
        self.plan = copy.deepcopy(plan)


class OutcomeUnknown(Exception):
    """The service raised after the call reached it. The effect may have
    landed. The idempotency key is frozen until an operator resolves it."""


@dataclass(frozen=True)
class Session:
    """Immutable session policy. Counters live inside the gateway."""
    actor: str
    allowed_tools: frozenset[str]
    max_mutations: int = 5
    max_bulk_scope: int = 3
    max_spend_cents: int = 50_000
    auto_approve_tiers: frozenset[Tier] = frozenset({Tier.READ, Tier.REVERSIBLE})

    def __post_init__(self) -> None:
        # Freeze collections passed in as sets or lists.
        object.__setattr__(self, "allowed_tools", frozenset(self.allowed_tools))
        object.__setattr__(self, "auto_approve_tiers", frozenset(self.auto_approve_tiers))
        # Configuration rule: the irreversible tier is never auto-approved.
        if Tier.IRREVERSIBLE in self.auto_approve_tiers:
            raise ValueError("the irreversible tier cannot be auto-approved")


@dataclass
class _SessionState:
    policy: Session
    mutations: int = 0
    spent_cents: int = 0


# Annotation strings used by MockService, mapped to accepted runtime types.
_TYPES = {"str": (str,), "int": (int,), "str | None": (str, type(None))}


def _type_errors(fn: Callable, args: dict) -> list[str]:
    """Arguments whose runtime type does not match the method annotation."""
    errors = []
    for name, p in inspect.signature(fn).parameters.items():
        if name not in args or p.annotation is inspect.Parameter.empty:
            continue
        accepted = _TYPES.get(p.annotation)
        value = args[name]
        if accepted is None:
            errors.append(f"{name}: unsupported annotation {p.annotation!r}")
        elif type(value) is bool or not isinstance(value, accepted):
            errors.append(f"{name}: expected {p.annotation}, "
                          f"got {type(value).__name__}")
    return errors


def _params(fn: Callable) -> tuple[set[str], set[str]]:
    """(required, allowed) parameter names for a service method."""
    sig = inspect.signature(fn)
    allowed = set(sig.parameters)
    required = {n for n, p in sig.parameters.items()
                if p.default is inspect.Parameter.empty}
    return required, allowed


class Gateway:
    def __init__(self, service: MockService,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.svc = service
        self.clock = clock
        self.audit: list[dict] = []
        self._sessions: dict[str, _SessionState] = {}
        # (actor, idempotency key) -> stored result. Scoped by actor, not by
        # session, so a crashed task retried in a new session still dedupes,
        # while two actors can never replay each other's results.
        self._idem: dict[tuple[str, str], dict] = {}
        self._plans: dict[str, dict] = {}      # plan id -> pending plan

    # -- sessions ---------------------------------------------------------
    def open(self, policy: Session) -> str:
        """Operator API: register a session policy, return an opaque ID.

        Not for agents. An agent that can call open() can grant itself any
        limits; a real gateway keeps this behind operator authentication.
        """
        sid = uuid.uuid4().hex[:8]
        self._sessions[sid] = _SessionState(policy)
        return sid

    # -- audit ------------------------------------------------------------
    def _log(self, sid: str, actor: str, tool: str, args, outcome: str) -> None:
        self.audit.append({"ts": time.time(), "session": sid, "actor": actor,
                           "tool": tool, "args": args, "outcome": outcome})

    # -- main entry point -------------------------------------------------
    def call(self, sid: str, tool: str, args: dict,
             idempotency_key: str | None = None,
             if_version: int | None = None,
             approved_plan: str | None = None):
        state = self._sessions.get(sid)
        if state is None:
            self._log(sid, "?", tool, args, "denied:unknown_session")
            raise Denied("unknown session")
        s = state.policy

        def deny(code: str, msg: str):
            self._log(sid, s.actor, tool, args, f"denied:{code}")
            raise Denied(msg)

        try:
            return self._call(sid, state, s, tool, args, idempotency_key,
                              if_version, approved_plan, deny)
        except (Denied, NeedsApproval, OutcomeUnknown):
            raise
        except Exception as e:  # any unexpected error before execution
            deny("bad_args", f"{tool}: rejected malformed input "
                             f"({type(e).__name__})")

    def _call(self, sid, state, s, tool, args, idempotency_key,
              if_version, approved_plan, deny):
        # 0. Unknown tools and malformed arguments fail closed
        if tool not in TOOL_TIERS:
            deny("unknown_tool", f"{tool} is not a known tool")
        if not isinstance(args, dict):
            deny("bad_args", "arguments must be an object")
        required, allowed = _params(getattr(self.svc, tool))
        missing, extra = required - set(args), set(args) - allowed
        if missing or extra:
            deny("bad_args", f"{tool}: missing {sorted(missing)}, "
                             f"unexpected {sorted(extra)}")
        type_errors = _type_errors(getattr(self.svc, tool), args)
        if type_errors:
            deny("bad_args", f"{tool}: " + "; ".join(type_errors))
        tier = TOOL_TIERS[tool]

        # 1. Capability scope: per task, not per agent
        if tool not in s.allowed_tools:
            deny("out_of_scope", f"{tool} is not in this session's toolbelt")

        if tier is Tier.READ:
            try:
                result = getattr(self.svc, tool)(**args)
            except Exception as e:  # reads have no effect to be in doubt about
                deny("service_error", f"{tool}: {type(e).__name__}")
            self._log(sid, s.actor, tool, args, "ok")
            return result

        # 2. Idempotency: every mutation carries a key; replays return the
        #    stored result instead of re-executing
        if idempotency_key is None:
            deny("no_idempotency_key", "mutations require an idempotency key")
        fingerprint = hashlib.sha256(
            json.dumps([tool, args], sort_keys=True).encode()).hexdigest()
        idem = (s.actor, idempotency_key)
        if idem in self._idem:
            stored = self._idem[idem]
            if stored.get("in_doubt"):
                deny("in_doubt", "earlier attempt with this key has an unknown "
                                 "outcome; an operator must verify provider "
                                 "state before it can run again")
            if stored["fingerprint"] != fingerprint:
                deny("key_reuse_mismatch",
                     "idempotency key reused with different arguments")
            self._log(sid, s.actor, tool, args, "replayed")
            return stored["result"]

        # 3. Freshness: compare-and-set against the version the agent read
        if "rid" in args:
            current = self.svc.resources.get(args["rid"])
            if current is None:
                deny("not_found", f"{args['rid']} does not exist")
            if if_version is None or current["version"] != if_version:
                deny("stale_precondition",
                     "precondition failed: resource changed since read")

        # 4. Budgets: mutation count and spend, enforced below the model
        if state.mutations >= s.max_mutations:
            deny("budget_exhausted", "session mutation budget exhausted")
        cents = 0
        if tool in TOOL_SPEND_FIELD:
            cents = args.get(TOOL_SPEND_FIELD[tool])
            # bool is a subclass of int, so exclude it explicitly
            if type(cents) is not int or cents < 0:
                deny("invalid_amount", f"{TOOL_SPEND_FIELD[tool]} must be a "
                                       f"non-negative integer, got {cents!r}")
        if state.spent_cents + cents > s.max_spend_cents:
            deny("spend_limit", f"spend of {cents} cents exceeds session limit "
                                f"({state.spent_cents}/{s.max_spend_cents} used)")

        # 5. Blast scope applies to every mutation, auto-approved or not
        scope = len(self._resolve(tool, args))
        if scope > s.max_bulk_scope:
            deny("blast_scope", f"plan touches {scope} resources, "
                                f"limit {s.max_bulk_scope}")

        # 6. Plan-then-apply for tiers that are not auto-approved
        plan = None
        if tier not in s.auto_approve_tiers:
            if approved_plan is None:
                self._purge_expired()
                new = self._plan(sid, s.actor, tool, args)
                self._plans[new["id"]] = new
                self._log(sid, s.actor, tool, args, "pending_approval")
                raise NeedsApproval(new)
            plan = self._plans.get(approved_plan)
            if plan is None or plan["fingerprint"] != fingerprint:
                deny("plan_mismatch", "approved plan does not match this call")
            if plan["session"] != sid:
                deny("plan_wrong_session", "approved plan belongs to another session")
            if self.clock() > plan["expires_at"]:
                del self._plans[approved_plan]
                deny("plan_expired", "approved plan expired: re-plan and re-approve")
            if not plan["approvals"]:
                deny("plan_not_approved", "plan has not been approved")
            # Bind the approval to its effect, not only its arguments:
            # re-resolve targets and refuse if any (id, version) drifted.
            if self._resolve(tool, args) != plan["targets"]:
                del self._plans[approved_plan]  # a drifted plan is dead
                deny("plan_drift", "world changed since approval: "
                                   "re-plan and re-approve")

        try:
            result = getattr(self.svc, tool)(**args)
        except Exception as e:
            # The call reached the service. Its effect may have landed.
            # Freeze the key, keep the plan, and refuse blind re-execution.
            # Count the attempt and its spend as if it landed: budgets must
            # hold in the worst case, not the hopeful one.
            state.mutations += 1
            state.spent_cents += cents
            self._idem[idem] = {"fingerprint": fingerprint, "in_doubt": True,
                                "tool": tool, "args": args, "sid": sid,
                                "cents": cents}
            self._log(sid, s.actor, tool, args, f"in_doubt:{type(e).__name__}")
            raise OutcomeUnknown(f"{tool} raised {type(e).__name__} after "
                                 f"reaching the service; outcome unknown") from e
        if plan is not None:
            del self._plans[plan["id"]]  # consumed only after success
        state.mutations += 1
        state.spent_cents += cents
        self._idem[idem] = {"fingerprint": fingerprint, "result": result}
        self._log(sid, s.actor, tool, args, "applied")
        return result

    def resolve_in_doubt(self, actor: str, key: str, landed: bool,
                         result=None) -> None:
        """Operator API: record what an in-doubt call actually did.

        landed=True stores the result so retries replay it. landed=False
        clears the key so the same intent can run again, and returns the
        mutation and spend that were counted while the outcome was unknown.
        """
        idem = (actor, key)
        entry = self._idem.get(idem)
        if entry is None or not entry.get("in_doubt"):
            raise Denied("no in-doubt call for that key")
        if landed:
            self._idem[idem] = {"fingerprint": entry["fingerprint"],
                                "result": result}
        else:
            del self._idem[idem]
            state = self._sessions.get(entry["sid"])
            if state is not None:
                state.mutations -= 1
                state.spent_cents -= entry["cents"]
        self.audit.append({"ts": time.time(), "session": "operator",
                           "actor": actor, "tool": entry["tool"],
                           "args": entry["args"],
                           "outcome": f"resolved:{'landed' if landed else 'not_landed'}"})

    def in_doubt_count(self) -> int:
        """How many calls currently have an unknown outcome."""
        return sum(1 for e in self._idem.values() if e.get("in_doubt"))

    def plan_view(self, plan_id: str) -> dict:
        """A copy of a pending plan, including its approval record."""
        plan = self._plans.get(plan_id)
        if plan is None:
            raise Denied("unknown plan")
        return copy.deepcopy(plan)

    # -- dry run: what would this call touch? -----------------------------
    def _resolve(self, tool: str, args: dict) -> list[tuple[str, int | None]]:
        """The concrete (id, version) pairs a call would touch right now."""
        if tool == "bulk_delete":
            return sorted((r["id"], r["version"]) for r in self.svc.list(args["tag"]))
        if "rid" in args:
            r = self.svc.resources.get(args["rid"])
            return [(args["rid"], r["version"] if r else None)]
        return [(args.get("order_id", "?"), None)]

    def _purge_expired(self) -> None:
        now = self.clock()
        for pid in [p for p, v in self._plans.items() if now > v["expires_at"]]:
            del self._plans[pid]

    def _plan(self, sid: str, actor: str, tool: str, args: dict) -> dict:
        targets = self._resolve(tool, args)
        fp = hashlib.sha256(json.dumps([tool, args], sort_keys=True).encode()).hexdigest()
        return {"id": uuid.uuid4().hex[:8], "tool": tool, "args": args,
                "targets": targets, "scope": len(targets), "fingerprint": fp,
                "session": sid, "actor": actor,
                "expires_at": self.clock() + PLAN_TTL_SECONDS,
                "approvals": []}  # append-only

    def approve(self, plan_id: str, approver: str) -> str:
        """Stand-in for an out-of-band human or policy approval.

        Rejects an approval under the requester's own name. Identities are
        plain strings, so this cannot stop an agent approving under another
        name; a real system authenticates the approver on a separate channel.
        """
        plan = self._plans.get(plan_id)
        if plan is None:
            raise Denied("unknown plan")
        if approver == plan["actor"]:
            raise Denied("an actor cannot approve its own plan")
        if self.clock() > plan["expires_at"]:
            raise Denied("plan expired")
        plan["approvals"].append({"approver": approver, "at": self.clock()})
        return plan_id
