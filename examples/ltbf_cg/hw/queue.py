"""queue.py -- the csynth build queue: fit, then held-out, then brute force (step 4.6).

Detached and resumable: a build whose ``report.json`` exists is skipped.  At most ``--workers``
builds at once and at most two at N_rx >= 256 (memory), each a ``python -m examples.ltbf_cg.hw.build``
subprocess under a 10 GB watchdog on its process tree.  Every report is copied to
``hw/reports/<name>.json`` and the build tree removed once harvested, keeping the disk bounded.

    python -m examples.ltbf_cg.hw.queue --roles fit heldout brute --workers 4
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

from examples.ltbf_cg.hw import build as hb
from examples.ltbf_cg.hw import space as sp

REPORTS = hb.BUILD.parent / "reports"
MEM_LIMIT = 10 * 2**30
BIG = 256
#: Builds kept for the RTL gates and the finalists: their trees are not removed.
KEEP = {
    "f_n64_c64_m16_r2x2_w16",
    "e_n64_c64_m16_r2x2_w16",
}


def _tree_rss(pid: int) -> int:
    import psutil

    try:
        p = psutil.Process(pid)
        procs = [p, *p.children(recursive=True)]
    except psutil.Error:
        return 0
    total = 0
    for q in procs:
        try:
            total += q.memory_info().rss
        except psutil.Error:
            pass
    return total


def run_one(k: sp.Knobs, big: threading.Semaphore) -> dict:
    """Build *k* (unless its report exists); return its record."""
    out = REPORTS / f"{k.name}.json"
    if out.is_file():
        return {"name": k.name, "skipped": True, **json.loads(out.read_text())}
    sem = big if k.nmax >= BIG else None
    if sem:
        sem.acquire()
    try:
        args = [sys.executable, "-m", "examples.ltbf_cg.hw.build"]
        for f in ("nmax", "cmax", "mmax", "R", "C", "W"):
            args += [f"--{f}", str(getattr(k, f))]
        if k.explicit:
            args.append("--explicit")
        t0 = time.monotonic()
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            start_new_session=True,
        )  # fmt: skip
        peak, killed = 0, False
        while proc.poll() is None:
            rss = _tree_rss(proc.pid)
            peak = max(peak, rss)
            if rss > MEM_LIMIT:
                os.killpg(proc.pid, signal.SIGKILL)
                killed = True
                break
            time.sleep(5)
        proc.wait()
        rep = hb.BUILD / k.name / "report.json"
        rec = (
            json.loads(rep.read_text())
            if rep.is_file()
            else {"name": k.name, "ok": False}
        )
        rec.update(
            {
                "wall_s": round(time.monotonic() - t0, 1),
                "peak_rss_gb": round(peak / 2**30, 2),
            }
        )
        if killed:
            rec.update({"ok": False, "error": "memory watchdog (10 GB)"})
        REPORTS.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rec, indent=1))
        if rec.get("ok") and k.name not in KEEP:
            shutil.rmtree(hb.BUILD / k.name, ignore_errors=True)
        return rec
    finally:
        if sem:
            sem.release()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--roles", nargs="+", default=["fit", "heldout", "brute"])
    p.add_argument("--workers", type=int, default=4)
    a = p.parse_args(argv)
    todo: list[sp.Knobs] = []
    for role in a.roles:
        for k in sp.load(role):
            if k not in todo:
                todo.append(k)
    print(f"{len(todo)} builds", flush=True)
    big = threading.Semaphore(2)
    t0, bad = time.monotonic(), 0
    with ThreadPoolExecutor(max_workers=a.workers) as pool:
        futs = {pool.submit(run_one, k, big): k for k in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            k = futs[fut]
            try:
                rec = fut.result()
            except Exception as exc:  # noqa: BLE001
                # one failed build must not stop the queue
                rec = {"ok": False, "error": repr(exc)}
            bad += not rec.get("ok")
            el = (time.monotonic() - t0) / 60
            print(
                f"[{i}/{len(todo)}] {el:.1f} min {k.name}: "
                f"{'skip' if rec.get('skipped') else ('ok' if rec.get('ok') else 'FAILED')} "
                f"{rec.get('csynth_s', '')} s {rec.get('error', '')}",
                flush=True,
            )
    print(f"done: {len(todo)} builds, {bad} failed", flush=True)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
