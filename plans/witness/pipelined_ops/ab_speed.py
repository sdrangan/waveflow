"""Stage 8, the wall-time half (plans/pipelined_ops.md) -- for a QUIET machine.

    python plans/witness/pipelined_ops/ab_speed.py --old 7167cfe3 --new main [--rounds 5] [--out ab.json]

Checks out *old* (the commit before the arc) and *new* into two temporary worktrees, then runs the
scripts behind docs/evaluation/sim_speed -- ``examples.mem_copy.mem_copy_runtime`` and
``examples.ssr_fft.ssr_fft_runtime`` (pysim only, no RTL) -- **alternating** old and new, each run in a
fresh process (the page's method: other load only ever adds time, so each point keeps its best).  Each
worktree imports its own code (``PYTHONPATH`` = the worktree).  Reports, per point, the best time of
each version and new/old, and whether both were bit-exact; writes everything to ``--out``.  Run from
the repo root, with the project's venv Python.  The event counts, the deterministic half, are
``event_counts.py`` beside this file.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

RUNS = {
    "mem_copy": (["-m", "examples.mem_copy.mem_copy_runtime", "--reps", "3"],
                 "examples/mem_copy/mem_copy_runtime.json"),
    "ssr_fft": (["-m", "examples.ssr_fft.ssr_fft_runtime", "--reps", "3"],
                "examples/ssr_fft/runtime.json"),
}


def _points(name: str, d: dict) -> dict[str, tuple[float, bool]]:
    """``{point: (seconds per unit, bit-exact)}`` from one run's JSON."""
    if name == "mem_copy":
        return {f"{p['n']}x{p['b']}": (p["pysim_us_per_copy"], p["bits_exact"]) for p in d["points"]}
    # Keyed by configuration; the runs here write L1024_pingpong (the script's defaults).  Any other
    # entry in the file is a committed earlier run, the same in both trees: not compared.
    run = d["L1024_pingpong"]
    return {f"L1024/{k}": (v["per_frame_ms"], v["bits_exact"]) for k, v in run.items()
            if k.startswith("pysim_") or k == "untimed"}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--old", required=True)
    ap.add_argument("--new", required=True)
    ap.add_argument("--rounds", type=int, default=5)
    ap.add_argument("--out", type=Path, default=Path("ab_speed.json"))
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="wf_ab_"))
    trees = {v: tmp / v for v in ("old", "new")}
    for v, ref in (("old", a.old), ("new", a.new)):
        subprocess.run(["git", "worktree", "add", "--quiet", "--detach", str(trees[v]), ref], check=True)
    best: dict = {name: {"old": {}, "new": {}} for name in RUNS}
    try:
        for r in range(a.rounds):
            for name, (args, result) in RUNS.items():
                for v in ("old", "new") if r % 2 == 0 else ("new", "old"):
                    env = {**os.environ, "PYTHONPATH": str(trees[v])}
                    subprocess.run([sys.executable, *args], cwd=trees[v], env=env, check=True,
                                   stdout=subprocess.DEVNULL)
                    d = json.loads((trees[v] / result).read_text(encoding="utf-8"))
                    for k, (t, ok) in _points(name, d).items():
                        prev = best[name][v].get(k)
                        best[name][v][k] = (min(t, prev[0]) if prev else t, ok and (prev[1] if prev else True))
            print(f"round {r + 1}/{a.rounds} done", flush=True)
    finally:
        for t in trees.values():
            subprocess.run(["git", "worktree", "remove", "--force", str(t)], check=False)
    report = {"old": a.old, "new": a.new, "rounds": a.rounds, "results": {}}
    for name in RUNS:
        for k in best[name]["old"]:
            (to, oko), (tn, okn) = best[name]["old"][k], best[name]["new"][k]
            report["results"][f"{name}/{k}"] = {"old": to, "new": tn, "ratio": tn / to,
                                                "bit_exact": oko and okn}
            print(f"{name:9s} {k:14s} old {to:10.2f}  new {tn:10.2f}  new/old {tn / to:6.3f}  "
                  f"exact={oko and okn}")
    a.out.write_text(json.dumps(report, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
