#!/usr/bin/env python3
"""Read-only validity checks for one live run of the live agent experiment.

Implements the "Validity checks" section of live/PROTOCOL.md and nothing
else. It reads a results folder and the frozen source tree. It writes
nothing, computes no safety or success rates, and draws no conclusions.

Usage
  python3 harness/validate_run.py data/<run_id> --root harness
  python3 harness/validate_run.py data/<run_id> --root harness --project 300 200

Exit code 0 if every applicable check passes, 1 otherwise.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

# config.json records code_sha256 by file name, first 16 hex characters.
FROZEN = {
    "gateway.py": "gateway.py",
    "tasks.py": "live/tasks.py",
    "live_agent.py": "live/live_agent.py",
    "PROTOCOL.md": "live/PROTOCOL.md",
}

results: list[tuple[str, str, str]] = []  # (status, check, detail)


def record(status: str, check: str, detail: str = "") -> None:
    results.append((status, check, detail))


def load(run: Path):
    cfg = json.loads((run / "config.json").read_text())
    rows = [json.loads(line) for line in
            (run / "episodes.jsonl").read_text().splitlines() if line.strip()]
    infra = json.loads((run / "infra_errors.json").read_text())
    return cfg, rows, infra


def check_hashes(cfg: dict, root: Path) -> None:
    recorded = cfg.get("code_sha256", {})
    bad = []
    for name, rel in FROZEN.items():
        actual = hashlib.sha256((root / rel).read_bytes()).hexdigest()[:16]
        if recorded.get(name) != actual:
            bad.append(f"{name}: run recorded {recorded.get(name)}, tree has {actual}")
    if bad:
        record("FAIL", "config hashes match the frozen files", "; ".join(bad))
    else:
        record("PASS", "config hashes match the frozen files",
               ", ".join(f"{n}={recorded[n]}" for n in FROZEN))


def check_accounting(cfg: dict, rows: list[dict], infra: list[dict]) -> None:
    expected = cfg["runs_per_task_per_arm"] * len(cfg["tasks"]) * len(cfg["arms"])
    ids = [r["episode_id"] for r in rows]
    dup = [i for i, c in Counter(ids).items() if c > 1]
    ok = len(rows) + len(infra) == expected and not dup
    record("PASS" if ok else "FAIL", "every planned episode is accounted for",
           f"planned {expected}, completed {len(rows)}, infra errors {len(infra)}"
           + (f", duplicate ids {dup}" if dup else ""))
    record("INFO", "infrastructure errors after the automatic rerun", str(len(infra)))


def total(rows: list[dict], arm: str, metric: str) -> int:
    return sum(r[metric] for r in rows if r["arm"] == arm)


def check_oracle_conditions(cfg: dict, rows: list[dict]) -> None:
    if cfg["approver"] != "oracle":
        record("SKIP", "oracle-conditioned checks",
               f"approver is {cfg['approver']}; these conditions only hold under oracle")
        return
    for arm in ("ask", "tiered"):
        if arm not in cfg["arms"]:
            continue
        for metric in ("bulk_violation", "stale_mutation"):
            n = total(rows, arm, metric)
            record("PASS" if n == 0 else "FAIL", f"{arm} (oracle): {metric} is zero",
                   f"{metric} total = {n}")
    if "ask" in cfg["arms"]:
        for metric in ("double_exec", "wrong_amount"):
            n = total(rows, "ask", metric)
            record("PASS" if n == 0 else "FAIL", f"ask (oracle): {metric} is zero",
                   f"{metric} total = {n}")


def check_identical_refunds(cfg: dict, rows: list[dict]) -> None:
    offenders = []
    for r in rows:
        if r["arm"] == "raw":
            continue
        seen = Counter((e["ids"][0], e["cents"]) for e in r["mutation_log"]
                       if e["op"] == "refund")
        if any(c > 1 for c in seen.values()):
            offenders.append(r["episode_id"])
    record("PASS" if not offenders else "FAIL",
           "gateway arms: no refund with identical arguments landed twice",
           "none" if not offenders else "episodes: " + ", ".join(offenders))


def project(rows: list[dict], targets: list[int]) -> None:
    n = len(rows)
    prompt = sum(r["tokens"]["prompt"] for r in rows)
    comp = sum(r["tokens"]["completion"] for r in rows)
    print("\nToken projection (arithmetic only)")
    print(f"  episodes in this run: {n}")
    if n == 0 or prompt + comp == 0:
        print("  token usage was not reported by the endpoint; no projection possible")
        return
    print(f"  measured tokens: prompt {prompt}, completion {comp}, total {prompt + comp}")
    for t in targets:
        print(f"  linear projection for {t} episodes: "
              f"prompt {round(prompt / n * t)}, completion {round(comp / n * t)}, "
              f"total {round((prompt + comp) / n * t)}")
    print("  Arms and tasks differ in length, so treat this as a rough scale, "
          "not a quote.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("run", type=Path, help="results/<run_id> folder")
    p.add_argument("--root", type=Path, required=True,
                   help="the harness folder (the one with gateway.py)")
    p.add_argument("--project", type=int, nargs="*", default=[],
                   help="target episode counts for a token projection")
    a = p.parse_args()

    cfg, rows, infra = load(a.run)
    print(f"Run {cfg['run_id']}: model {cfg['model']}, approver {cfg['approver']}, "
          f"runs per task per arm {cfg['runs_per_task_per_arm']}, "
          f"arms {','.join(cfg['arms'])}")
    check_hashes(cfg, a.root)
    check_accounting(cfg, rows, infra)
    check_oracle_conditions(cfg, rows)
    check_identical_refunds(cfg, rows)

    print()
    for status, check, detail in results:
        print(f"[{status}] {check}" + (f": {detail}" if detail else ""))
    if a.project:
        project(rows, a.project)

    failed = [r for r in results if r[0] == "FAIL"]
    print(f"\nRESULT: {'INVALID RUN, ' + str(len(failed)) + ' check(s) failed' if failed else 'all applicable checks passed'}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
