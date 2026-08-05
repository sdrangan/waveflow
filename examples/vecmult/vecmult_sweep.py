"""vecmult_sweep.py — run the grid of design points through the DAG and record the counters.

The corpus behind ``docs/guide/resource_model/example.md``.  The grid is chosen to separate the two
BRAM regimes rather than to cover a box uniformly:

* ``vlen = 512``   — banks far shallower than a BRAM18; the LUTRAM corner lives here.
* ``vlen = 1024``  — every bank rounds up to exactly one block, so BRAM should track ``LW``.
* ``vlen = 4096``  — straddles: partition-bound at large ``LW``, data-bound at small.
* ``vlen = 16384`` — banks deeper than a block, so BRAM should be **data-bound** and independent
  of ``LW``.  This column is the one that distinguishes a ceiling law from ``BRAM = LW``.

Each point is one csynth (~20 s), so the whole grid is a few minutes.

    python -m examples.vecmult.vecmult_sweep --dry-run    # codegen only, no Vitis
    python -m examples.vecmult.vecmult_sweep
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

from waveflow.build.build import BuildConfig  # noqa: E402

from examples.vecmult.vecmult import SAMP_W, lane_width  # noqa: E402
from examples.vecmult.vecmult_build import build_vecmult_dag  # noqa: E402

DWIDS = (32, 64, 128, 256)
VLENS = (512, 1024, 4096, 16384)

#: The work-tier platform this sweep accumulates into.  Deliberately **not** the name of a tracked
#: library: a sweep is exploratory and re-runs freely, so it writes to the untracked ``calib/work/``
#: tier and a deliberate ``publish`` promotes the result:
#:
#:     waveflow_calib publish examples/vecmult/calib/work/zynq7020_vecmult_sweep #:                            examples/vecmult/calib/platforms/zynq7020_vecmult --apply
#:
#: Setting it at all is what makes the sweep produce a **record store** rather than only a summary
#: JSON.  Without it ``InspectSynthStep`` attributes the report and has nowhere shared to file it, so
#: the measurements survive only as numbers a human copies into source -- which is exactly how this
#: example's corpus started life.
PLATFORM = "zynq7020_vecmult_sweep"
PLATFORMS_ROOT = HERE / "calib" / "work"
PART = "xc7z020clg484-1"
CLK_FREQ = 100e6

SUMMARY = HERE / "results" / "sweep.json"


def points(dwids=DWIDS, vlens=VLENS) -> list[dict]:
    return [{"dwid": d, "vlen": v} for v in vlens for d in dwids]


def label(p: dict) -> str:
    return f"vlen{p['vlen']}_dw{p['dwid']}"


def run_point(p: dict, *, through: str, use_platform: bool = True) -> dict:
    """Run one design point through the DAG; return a record (never raises).

    *use_platform* off is for a ``--dry-run``, which never synthesizes and so has no report to file.
    """
    started = time.perf_counter()
    try:
        cfg_kw = dict(root_dir=HERE, params={**p, "live_output": False})
        if use_platform:
            cfg_kw.update(platform=PLATFORM, platforms_root=PLATFORMS_ROOT,
                          part=PART, clk_freq=CLK_FREQ)
        config = BuildConfig(**cfg_kw)
        results = build_vecmult_dag().run(config, through=through, force=True)
    except Exception as exc:                    # a point that blows up is data, not a stop
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "elapsed": round(time.perf_counter() - started, 2), **p}

    failed = {n: r.message for n, r in results.items() if not r.success}
    out = {"ok": not failed, "elapsed": round(time.perf_counter() - started, 2),
           "lw": lane_width(p["dwid"]), "samp_w": SAMP_W, **p}
    if failed:
        out["error"] = "; ".join(f"{n}: {m}" for n, m in failed.items())
    elif through == "resources":
        res = HERE / "results" / "resources.json"
        if res.is_file():
            blob = json.loads(res.read_text(encoding="utf-8"))
            out["top"] = blob.get("top", {})
            out["module_sum"] = blob.get("module_sum", {})
            out["integration"] = blob.get("integration", {})
    return out


def _save(recs: dict, started: float, complete: bool) -> None:
    """Write the summary after **every** point, not only at the end.

    A grid of 16 csynths is ~10 minutes, which is easily long enough to be interrupted; writing only
    at the end means an interruption at point 15 saves nothing *and* leaves the previous run's file
    in place, which is worse than saving nothing — a stale corpus that reads as a fresh one.
    ``complete`` records whether the grid actually finished, so a partial file cannot be mistaken
    for a whole one.
    """
    SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY.write_text(json.dumps({
        "grid": {"dwid": list(DWIDS), "vlen": list(VLENS)},
        "samp_w": SAMP_W,
        "complete": complete,
        "n_points": len(recs),
        "total_seconds": round(time.perf_counter() - started, 1),
        "points": recs,
    }, indent=2), encoding="utf-8")


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="Sweep vecmult resource points through csynth.")
    ap.add_argument("--dry-run", action="store_true",
                    help="stop at codegen_dut (no Vitis) — a cheap pre-flight over the whole grid")
    ap.add_argument("--resume", action="store_true",
                    help="skip points already recorded as ok in the existing summary")
    args = ap.parse_args(argv)

    through = "codegen_dut" if args.dry_run else "resources"
    grid = points()
    recs: dict = {}
    if args.resume and SUMMARY.is_file():
        prev = json.loads(SUMMARY.read_text(encoding="utf-8")).get("points", {})
        recs = {k: v for k, v in prev.items() if v.get("ok")}
        print(f"resuming: {len(recs)} point(s) already recorded")
    started = time.perf_counter()
    for i, p in enumerate(grid, 1):
        if label(p) in recs:
            print(f"[{i}/{len(grid)}] {label(p)} — skipped (already ok)")
            continue
        print(f"[{i}/{len(grid)}] {label(p)} ...", flush=True)
        rec = run_point(p, through=through, use_platform=not args.dry_run)
        recs[label(p)] = rec
        if not rec["ok"]:
            print(f"    FAILED: {rec.get('error')}")
        elif "top" in rec:
            t = rec["top"]
            print(f"    LW={rec['lw']:<3} bram={t.get('bram')} dsp={t.get('dsp')} "
                  f"ff={t.get('ff')} lut={t.get('lut')}")
        if not args.dry_run:
            _save(recs, started, complete=False)

    if not args.dry_run:
        _save(recs, started, complete=len(recs) == len(grid))
        print(f"\nwrote {SUMMARY.relative_to(HERE)}")
    return 0 if all(r["ok"] for r in recs.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
