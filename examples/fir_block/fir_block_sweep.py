"""fir_block_sweep.py — the B2 parameter sweep: one csynth per design point, records accumulated.

Phase B2 of ``plans/resource_model.md``.  Drives the ``fir_block`` DAG through ``resources`` once per
point in a ``ntap x samp_w x realization`` grid, so the module store ends up holding a real corpus for
the priors (D1) and the composition fit (E1) to be tested against.

Three things this does that a shell loop would not:

* **Writes to the work tier.**  The sweep churns and its numbers are unproven, so it lands in an
  untracked ``calib/work/`` platform under its own name.  Using the shipped platform's name would make
  ``Platform.resolve`` find the *packaged* directory through its fallbacks and write into the tracked
  library, which only ``publish_calib`` may do.
* **Never silently drops a point.**  A csynth that fails is recorded as a failure with its error, not
  skipped.  A sweep that quietly covered 19 of 24 points, reported as 24, would put a hole in the
  fitted region exactly where an agent would later be told it was interpolating.
* **Writes incrementally and resumes.**  Hours of synthesis should not be lost to one crash near the
  end, and ``--resume`` skips points already recorded.

Usage::

    python -m examples.fir_block.fir_block_sweep --dry-run     # elaborate + codegen only, no Vitis
    python -m examples.fir_block.fir_block_sweep               # the full sweep
    python -m examples.fir_block.fir_block_sweep --resume      # continue an interrupted one
"""
from __future__ import annotations

import argparse
import json
import time
from itertools import product
from pathlib import Path

from waveflow.build.build import BuildConfig

from examples.fir_block.fir_block import DEFAULT_SAMP_I, MEM_DW
from examples.fir_block.fir_block_build import build_fir_block_dag

HERE = Path(__file__).resolve().parent

#: The grid.  ``ntap`` and ``samp_w`` vary independently (a grid, not a diagonal) or their
#: coefficients cannot be separated; ``unroll_lane`` is a *realization*, not a feature — it forks the
#: module key and therefore gets its own model rather than becoming a regression column.
NTAPS = (8, 16, 32)
SAMP_WS = (8, 12, 16, 24)
REALIZATIONS = (False, True)

#: The work-tier platform this sweep accumulates into.  Deliberately NOT the shipped
#: ``zynq7020_bfm_100mhz`` name — see the module docstring.
PLATFORM = "zynq7020_fir_sweep"
PLATFORMS_ROOT = "calib/work"
PART = "xc7z020clg484-1"
CLK_FREQ = 100e6                      # the 10 ns period the generated TCL creates

SUMMARY = HERE / "results" / "sweep.json"


def points() -> list[dict]:
    """The grid as a list of parameter dicts, in a stable order."""
    return [{"ntap": n, "samp_w": w, "samp_i": DEFAULT_SAMP_I,
             "mem_dwidth": MEM_DW, "unroll_lane": u}
            for n, w, u in product(NTAPS, SAMP_WS, REALIZATIONS)]


def label(p: dict) -> str:
    return f"ntap{p['ntap']}_w{p['samp_w']}_{'unroll' if p['unroll_lane'] else 'serial'}"


def load_summary() -> dict:
    if SUMMARY.is_file():
        return json.loads(SUMMARY.read_text(encoding="utf-8"))
    return {"points": {}}


def save_summary(data: dict) -> None:
    SUMMARY.parent.mkdir(parents=True, exist_ok=True)
    SUMMARY.write_text(json.dumps(data, indent=2), encoding="utf-8")


def run_point(p: dict, *, through: str, use_platform: bool) -> dict:
    """Run one design point through the DAG; return a result record (never raises)."""
    cfg_kw = dict(root_dir=HERE, params={**p, "live_output": False})
    if use_platform:
        cfg_kw.update(platform=PLATFORM, platforms_root=PLATFORMS_ROOT,
                      part=PART, clk_freq=CLK_FREQ)
    started = time.perf_counter()
    try:
        config = BuildConfig(**cfg_kw)
        results = build_fir_block_dag().run(config, through=through, force=True)
    except Exception as exc:                       # a point that blows up is data, not a stop
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "elapsed": time.perf_counter() - started, **p}

    failed = {n: r.message for n, r in results.items() if not r.success}
    per_step = {n: round(r.elapsed_seconds, 2) for n, r in results.items() if not r.skipped}
    out = {"ok": not failed, "elapsed": round(time.perf_counter() - started, 2),
           "steps": per_step, **p}
    if failed:
        out["error"] = "; ".join(f"{n}: {m}" for n, m in failed.items())
    else:
        res = HERE / "results" / "resources.json"
        if res.is_file():
            blob = json.loads(res.read_text(encoding="utf-8"))
            out["top"] = blob.get("top", {})
            out["module_sum"] = blob.get("module_sum", {})
            out["integration"] = blob.get("integration", {})
            out["modules"] = {m["cls_name"]: {"key": m["key"], "resources": m["resources"]}
                              for m in blob.get("modules", [])}
    return out


def main(argv: "list[str] | None" = None) -> int:
    ap = argparse.ArgumentParser(description="Sweep fir_block resource points through csynth.")
    ap.add_argument("--dry-run", action="store_true",
                    help="stop at codegen_dut (no Vitis) — a cheap pre-flight over the whole grid")
    ap.add_argument("--resume", action="store_true", help="skip points already recorded as ok")
    args = ap.parse_args(argv)

    through = "codegen_dut" if args.dry_run else "resources"
    grid = points()
    summary = load_summary() if args.resume else {"points": {}}
    summary.setdefault("points", {})
    summary["grid"] = {"ntap": list(NTAPS), "samp_w": list(SAMP_WS),
                       "unroll_lane": list(REALIZATIONS)}
    summary["platform"] = None if args.dry_run else PLATFORM

    todo = [p for p in grid
            if not (args.resume and summary["points"].get(label(p), {}).get("ok"))]
    print(f"fir_block sweep: {len(todo)} of {len(grid)} point(s), through '{through}'")
    if not args.dry_run:
        print(f"  platform {PLATFORM} under {PLATFORMS_ROOT} (work tier, untracked)")

    t0 = time.perf_counter()
    for i, p in enumerate(todo, 1):
        name = label(p)
        print(f"[{i}/{len(todo)}] {name} ... ", end="", flush=True)
        rec = run_point(p, through=through, use_platform=not args.dry_run)
        summary["points"][name] = rec
        save_summary(summary)                       # incremental: a crash costs one point, not all
        if rec["ok"]:
            top = rec.get("top", {})
            extra = (f" lut={top.get('lut')} dsp={top.get('dsp')} bram={top.get('bram')}"
                     if top else "")
            print(f"ok  {rec['elapsed']:.1f}s{extra}")
        else:
            print(f"FAILED  {rec.get('error', '')[:120]}")

    ok = [r for r in summary["points"].values() if r.get("ok")]
    bad = [r for r in summary["points"].values() if not r.get("ok")]
    summary["total_seconds"] = round(time.perf_counter() - t0, 1)
    save_summary(summary)

    print(f"\n{len(ok)} ok, {len(bad)} failed, {summary['total_seconds']:.0f}s total")
    for r in bad:
        print(f"  FAILED {label(r)}: {r.get('error', '')[:160]}")
    print(f"summary -> {SUMMARY}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
