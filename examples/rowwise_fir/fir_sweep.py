"""fir_sweep.py — streaming-pipeline cosim sweep + per-stage span extraction (Stage B).

Replaces the retired matrix-LT ``fir_validate.py`` / ``fir_calibrate.py`` (which referenced the
old ``fir_build.generate(N,N)`` / ``fir_gen_proj`` build).  Drives the **current** free-running
build (``fir_build.generate()`` -> ``waveflow_fir_proj``, top ``fir``) and, per size point, cosims
``NJOBS`` identical jobs back-to-back so a single run yields the whole streaming timing story:

  * **single-job latency** L1  — total RTL cycles when NJOBS==1 (or the first job's bus extent)
  * **steady per-job period** P — the inter-job spacing of the X-read bursts (and == total-cycle
    increment per added job), the throughput knob the freerun pipeline buys (cross-job overlap)
  * **per-stage spans**        — X-read burst span (``load``), Y-write burst span (``store``),
    and the fill = (first Y-write start − first X-read start) (the load->compute->store latency)

extracted from the m_axi VCD bursts (reads and writes share the ``gmem`` bundle; the parser
separates them).  Bursts are attributed to jobs by address range (the tb lays jobs out
contiguously and deterministically — replicated in :func:`job_layout`).

Usage (project venv; Vitis 2025.1 + Vivado xsim)::

    # one point (de-risk the extraction)
    PYTHONPATH=../../.. ../../../pysilicon-venv/Scripts/python.exe fir_sweep.py --point 4 64 --njobs 3
    # the full grid -> results/cosim_sweep.json
    PYTHONPATH=../../.. ../../../pysilicon-venv/Scripts/python.exe fir_sweep.py --sweep
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))

from waveflow.utils.cosimparse import CosimReportParser        # noqa: E402

import examples.rowwise_fir.run_fir as run_fir                 # noqa: E402
from examples.rowwise_fir.fir_golden import T                  # noqa: E402

TCL = HERE / "run.tcl"
SOL = HERE / "waveflow_fir_proj" / "solution1"
PROJ = "waveflow_fir_proj"
TOP = "fir"
RESULTS = HERE / "results"
SWEEP_JSON = RESULTS / "cosim_sweep.json"
WORD_BYTES = 4                       # float32 = one 32-bit m_axi word

# Size grid (mirrors the sandbox sweep); the holdout is INTERIOR-ish and untrained.
GRID = [(1, 16), (1, 32), (1, 64), (1, 128), (1, 256),
        (4, 16), (4, 32), (4, 64), (4, 128), (4, 256)]
HOLDOUT = (2, 96)                    # untrained (n_row AND n_col both off the grid)
NJOBS = 3                            # back-to-back jobs per point (>=2 to see the steady period)


def out_len(n_col: int) -> int:
    return n_col - T + 1


def job_layout(n_row: int, n_col: int, n_jobs: int) -> list[dict]:
    """Replicate fir_tb.cpp::layout_and_fill — contiguous X | h | Y per job (element offsets)."""
    jobs, off = [], 0
    ol = out_len(n_col)
    for i in range(n_jobs):
        x_off = off
        h_off = x_off + n_row * n_col
        y_off = h_off + T
        off = y_off + n_row * ol
        jobs.append({"job": i, "x_off": x_off, "h_off": h_off, "y_off": y_off,
                     "x_words": n_row * n_col, "y_words": n_row * ol})
    return jobs


def _burst_beats(b: dict) -> int:
    return sum(1 for bt in b.get("beat_type", []) if bt == 0)


def _attribute(bursts: list[dict], lo_words: int, hi_words: int) -> list[dict]:
    """Bursts whose address falls in the byte range [lo,hi) of one job's region."""
    lo, hi = lo_words * WORD_BYTES, hi_words * WORD_BYTES
    return [b for b in bursts if lo <= int(b["addr"]) < hi]


def _span(bursts: list[dict]) -> dict | None:
    if not bursts:
        return None
    t0 = min(float(b.get("data_tstart", b["tstart"])) for b in bursts)
    t1 = max(float(b["data_tend"]) for b in bursts)
    return {"start_ns": t0, "end_ns": t1, "words": sum(_burst_beats(b) for b in bursts)}


def cosim_point(n_row: int, n_col: int, n_jobs: int = NJOBS) -> dict:
    """Cosim NJOBS identical (n_row,n_col) jobs; return total cycles + per-job/steady spans."""
    from vcdvcd import VCDVCD
    from waveflow.scripts.xsim_vcd import run_xsim_vcd
    from waveflow.utils.vcd import VcdParser

    scenario = f"sweep:{n_row}:{n_col}:{n_jobs}"
    run_fir.run(scenario, cosim=True, trace="port")
    total_cyc = CosimReportParser(sol_path=str(SOL), top=TOP).get_transaction_cycles()

    vcd_path = run_xsim_vcd(top=TOP, comp=PROJ, out="dump.vcd", soln="solution1",
                            trace_level="port", workdir=HERE)
    vp = VcdParser(VCDVCD(str(vcd_path), signals=None, store_tvs=True))
    clk = vp.add_clock_signal()
    aximm_sigs, _ = vp.add_aximm_signals(prefix="m_axi_gmem_", dir="both",
                                         lite_only=False, short_name_prefix="gmem_")
    write_bursts, read_bursts, clk_ns = vp.extract_aximm_bursts(clk_name=clk, aximm_sigs=aximm_sigs)
    clk_ns = float(clk_ns)

    layout = job_layout(n_row, n_col, n_jobs)
    per_job = []
    for j in layout:
        rd = _attribute(read_bursts, j["x_off"], j["x_off"] + j["x_words"] + T)  # X + h
        wr = _attribute(write_bursts, j["y_off"], j["y_off"] + j["y_words"])
        rs, ws = _span(rd), _span(wr)
        per_job.append({"job": j["job"],
                        "load_start_ns": rs["start_ns"] if rs else None,
                        "load_span_cyc": (rs["end_ns"] - rs["start_ns"]) / clk_ns if rs else None,
                        "load_words": rs["words"] if rs else 0,
                        "store_start_ns": ws["start_ns"] if ws else None,
                        "store_span_cyc": (ws["end_ns"] - ws["start_ns"]) / clk_ns if ws else None,
                        "store_words": ws["words"] if ws else 0,
                        "fill_cyc": (ws["start_ns"] - rs["start_ns"]) / clk_ns
                        if (rs and ws) else None})

    # Steady per-job period = the inter-job spacing of the BOTTLENECK stage.  Loads run ahead
    # (cross-job overlap), so the load-start spacing under-reads; the store-start spacing is the
    # throughput period (== the total-cycle increment per added job; cf. three-two=704 @ 4x64).
    def spacing(key: str) -> float | None:
        st = [p[key] for p in per_job if p[key] is not None]
        if len(st) < 2:
            return None
        return sum((st[i + 1] - st[i]) / clk_ns for i in range(len(st) - 1)) / (len(st) - 1)

    load_period = spacing("load_start_ns")
    store_period = spacing("store_start_ns")
    period_cyc = store_period if store_period is not None else load_period
    # Single-job latency = first job's completion (store end) from sim start.
    pj0 = per_job[0]
    l1_cyc = None
    if pj0["store_start_ns"] is not None and pj0["store_span_cyc"] is not None:
        l1_cyc = pj0["store_start_ns"] / clk_ns + pj0["store_span_cyc"]

    return {
        "n_row": n_row, "n_col": n_col, "n_jobs": n_jobs, "clk_ns": clk_ns,
        "trips": n_row * out_len(n_col),
        "read_words": n_row * n_col + T, "write_words": n_row * out_len(n_col),
        "total_cycles": int(total_cyc) if total_cyc is not None else None,
        "period_cyc": round(period_cyc, 1) if period_cyc is not None else None,
        "load_period_cyc": round(load_period, 1) if load_period is not None else None,
        "single_job_latency_cyc": round(l1_cyc, 1) if l1_cyc is not None else None,
        "per_job": per_job,
    }


def run_sweep(points: list[tuple[int, int]], njobs: int) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    run_fir.codegen_and_patch()
    out: list[dict] = []
    for nr, nc in points:
        print(f"[sweep {nr}x{nc} x{njobs}] cosim ...", flush=True)
        p = cosim_point(nr, nc, njobs)
        out.append(p)
        pj0 = p["per_job"][0]
        print(f"  total={p['total_cycles']} period={p['period_cyc']} (load_period="
              f"{p['load_period_cyc']}) L1={p['single_job_latency_cyc']} "
              f"load_span={pj0['load_span_cyc']} store_span={pj0['store_span_cyc']}", flush=True)
        SWEEP_JSON.write_text(json.dumps({"njobs": njobs, "T": T, "holdout": list(HOLDOUT),
                                          "points": out}, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {SWEEP_JSON}")


def main() -> None:
    ap = argparse.ArgumentParser(description="Streaming FIR cosim sweep (Stage B).")
    ap.add_argument("--point", nargs=2, type=int, metavar=("NROW", "NCOL"),
                    help="cosim a single point (de-risk the extraction)")
    ap.add_argument("--njobs", type=int, default=NJOBS)
    ap.add_argument("--sweep", action="store_true", help="cosim the full grid + holdout")
    args = ap.parse_args()

    if args.point:
        run_fir.codegen_and_patch()
        p = cosim_point(args.point[0], args.point[1], args.njobs)
        print(json.dumps(p, indent=2))
        return
    if args.sweep:
        run_sweep(GRID + [HOLDOUT], args.njobs)
        return
    ap.error("pass --point NROW NCOL or --sweep")


if __name__ == "__main__":
    main()
