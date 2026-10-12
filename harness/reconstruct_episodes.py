#!/usr/bin/env python3
"""Rebuild an empty episodes.jsonl from the per-episode transcripts, and prove
the rebuild is faithful before writing anything.

Why this is safe: the runner writes each finished episode twice, once as
transcripts/<episode_id>.json (the full row) and once as one line of
episodes.jsonl (the same row without its "transcript" key). It then computes
summary.md from the in-memory rows. So the transcripts hold everything, and
summary.md is an independent check: if recomputing the summary from the
rebuilt rows reproduces summary.md exactly, the rebuilt rows are the rows the
runner had in memory.

Checks, all of which must pass before any file is written:
  1. episodes.jsonl exists and is empty (a non-empty file is never touched).
  2. The transcripts are exactly the planned episodes (task x arm x run from
     config.json) minus the infra errors listed in infra_errors.json, with no
     extras, no duplicates, and each file's episode_id matching its name.
  3. summarize(rebuilt rows), using the frozen live_agent.py, equals the body
     of the run's summary.md, and the header's infra error count matches.

On success it writes into the run folder only:
  episodes.jsonl.empty-original   copy of the empty file, kept as evidence
  episodes.jsonl                  the rebuilt rows (planned order, not completion order)
  RECONSTRUCTION_NOTE.txt         what was done, with hashes

Usage
  python3 harness/reconstruct_episodes.py data/<run_id> --root harness
  python3 harness/reconstruct_episodes.py data/<run_id> --root harness --check-only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path


def fail(msg: str) -> None:
    print(f"REFUSED: {msg}")
    sys.exit(1)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("run", type=Path)
    p.add_argument("--root", type=Path, required=True, help="the harness folder (the one with gateway.py)")
    p.add_argument("--check-only", action="store_true", help="verify, write nothing")
    a = p.parse_args()
    run, root = a.run, a.root

    cfg = json.loads((run / "config.json").read_text())
    infra = json.loads((run / "infra_errors.json").read_text())
    ep_path = run / "episodes.jsonl"

    # 1. the file must be empty
    if not ep_path.exists():
        fail("episodes.jsonl does not exist")
    if ep_path.stat().st_size != 0:
        fail(f"episodes.jsonl is not empty ({ep_path.stat().st_size} bytes); never overwritten")

    # 2. transcripts are exactly the planned episodes minus infra errors
    planned = [f"{t}-{arm}-{i:03d}" for t in cfg["tasks"] for arm in cfg["arms"]
               for i in range(cfg["runs_per_task_per_arm"])]
    infra_ids = {f"{e['task']}-{e['arm']}-{e['run']:03d}" for e in infra}
    expected = [e for e in planned if e not in infra_ids]
    files = sorted((run / "transcripts").glob("*.json"))
    found = {f.stem for f in files}
    missing, extra = sorted(set(expected) - found), sorted(found - set(expected))
    if missing or extra or len(files) != len(found):
        fail(f"transcripts do not match the plan: missing {missing[:5]}, extra {extra[:5]}")
    rows_by_id = {}
    for f in files:
        row = json.loads(f.read_text())
        if row.get("episode_id") != f.stem:
            fail(f"{f.name} contains episode_id {row.get('episode_id')!r}")
        rows_by_id[f.stem] = row
    rows = [rows_by_id[e] for e in expected]
    print(f"transcripts: {len(rows)} of {len(planned)} planned, {len(infra_ids)} infra errors: OK")

    # 3. recompute the summary with the frozen code and compare with the runner's summary.md
    sys.path.insert(0, str(root / "live"))
    sys.path.insert(0, str(root))
    import live_agent  # the frozen runner module; main() is not executed on import

    rebuilt_summary = live_agent.summarize(rows)
    parts = (run / "summary.md").read_text().split("\n\n", 2)
    if len(parts) != 3:
        fail("summary.md does not have the expected header")
    header, body = parts[1], parts[2]
    if f"Infra errors (excluded, listed in infra_errors.json): {len(infra)}." not in header:
        fail("summary.md header infra error count does not match infra_errors.json")
    if body.strip() != rebuilt_summary.strip():
        fail("summary recomputed from the transcripts differs from the run's summary.md")
    print("summary: recomputed from the transcripts, identical to the run's own summary.md: MATCH")

    if a.check_only:
        print("check-only: nothing written")
        return

    # write
    shutil.copy2(ep_path, run / "episodes.jsonl.empty-original")
    lines = [json.dumps({k: v for k, v in r.items() if k != "transcript"}, default=str)
             for r in rows]
    ep_path.write_text("\n".join(lines) + "\n")
    digest = hashlib.sha256(ep_path.read_bytes()).hexdigest()
    (run / "RECONSTRUCTION_NOTE.txt").write_text(
        "episodes.jsonl was empty after the run, although the runner logged every episode.\n"
        f"Rebuilt on {time.strftime('%Y-%m-%d %H:%M:%S %Z')} by reconstruct_episodes.py from "
        f"{len(rows)} transcripts.\n"
        "Rows are in planned order, not completion order. Each line is the transcript row "
        "without its 'transcript' key.\n"
        "Verified: summarize(rebuilt rows) with the frozen live_agent.py equals this run's "
        "summary.md exactly.\n"
        f"episodes.jsonl sha256: {digest}\n"
        "The original empty file is kept as episodes.jsonl.empty-original.\n")
    print(f"wrote episodes.jsonl ({len(lines)} rows, sha256 {digest[:16]}...), "
          "episodes.jsonl.empty-original, RECONSTRUCTION_NOTE.txt")


if __name__ == "__main__":
    main()
