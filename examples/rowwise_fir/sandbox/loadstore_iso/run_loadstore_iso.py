"""run_loadstore_iso.py — Rung 2: free-running load||store (no compute), multi-job.

Cosims NJOBS copy jobs of N words on one gmem bundle, port-traces the VCD, and asks: does
load(job N)'s read burst overlap store(job N-1)'s write burst in time?

  period -> ~max(N,N) = N   => the free-running per-job structure DOES exploit full-duplex
  period -> ~N+N = 2N       => it serializes read+write (the FIR's behavior) — so the per-job/
                               burst structure itself is the cause, independent of compute

Run (project venv, Vitis 2025.1 + xsim)::

    PYTHONPATH=../../../.. ../../../../pysilicon-venv/Scripts/python.exe run_loadstore_iso.py --n 256 --nj 3
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
sys.path.insert(0, str(REPO))

from waveflow.toolchain import toolchain                  # noqa: E402
from waveflow.utils.cosimparse import CosimReportParser   # noqa: E402

TCL = HERE / "loadstore_iso.tcl"
SOL = HERE / "loadstore_iso_proj" / "solution1"
WORD_BYTES = 4


def _beats(b: dict) -> int:
    return sum(1 for bt in b.get("beat_type", []) if bt == 0)


def _attr(bursts, lo_w, hi_w):
    lo, hi = lo_w * WORD_BYTES, hi_w * WORD_BYTES
    sel = [b for b in bursts if lo <= int(b["addr"]) < hi]
    if not sel:
        return None
    t0 = min(float(b.get("data_tstart", b["tstart"])) for b in sel)
    t1 = max(float(b["data_tend"]) for b in sel)
    return t0, t1, sum(_beats(b) for b in sel)


def run(n: int, nj: int) -> dict:
    from vcdvcd import VCDVCD
    from waveflow.scripts.xsim_vcd import run_xsim_vcd
    from waveflow.utils.vcd import VcdParser

    env = {"WAVEFLOW_LS_N": str(n), "WAVEFLOW_LS_NJ": str(nj)}
    last = ""
    for _ in (1, 2):
        res = toolchain.run_vitis_hls_result(TCL, work_dir=HERE, capture_output=True, env=env)
        last = (res.get("stdout") or "") + (res.get("stderr") or "")
        if "WAVEFLOW_SUCCESS" in last:
            break
    if "WAVEFLOW_SUCCESS" not in last:
        raise RuntimeError(f"cosim failed:\n{last[-2500:]}")
    total = CosimReportParser(sol_path=str(SOL), top="loadstore_iso").get_transaction_cycles()

    vcd = run_xsim_vcd(top="loadstore_iso", comp="loadstore_iso_proj", out="dump.vcd",
                       soln="solution1", trace_level="port", workdir=HERE)
    vp = VcdParser(VCDVCD(str(vcd), signals=None, store_tvs=True))
    clk = vp.add_clock_signal()
    sigs, _ = vp.add_aximm_signals(prefix="m_axi_gmem_", dir="both", lite_only=False,
                                   short_name_prefix="gmem_")
    wbursts, rbursts, clk_ns = vp.extract_aximm_bursts(clk_name=clk, aximm_sigs=sigs)
    clk_ns = float(clk_ns)

    jobs = []
    for j in range(nj):
        xoff, yoff = 2 * j * n, 2 * j * n + n
        rd = _attr(rbursts, xoff, xoff + n)
        wr = _attr(wbursts, yoff, yoff + n)
        jobs.append({"j": j,
                     "load": (rd[0] / clk_ns, rd[1] / clk_ns) if rd else None,
                     "store": (wr[0] / clk_ns, wr[1] / clk_ns) if wr else None})

    # overlap of load(N) read window with store(N-1) write window (cycles), and steady period
    overlaps, store_ends = [], []
    for j in range(nj):
        if jobs[j]["store"]:
            store_ends.append(jobs[j]["store"][1])
        if j >= 1 and jobs[j]["load"] and jobs[j - 1]["store"]:
            l0, l1 = jobs[j]["load"]
            s0, s1 = jobs[j - 1]["store"]
            overlaps.append(max(0.0, min(l1, s1) - max(l0, s0)))
    period = None
    if len(store_ends) >= 3:
        sp = [store_ends[i + 1] - store_ends[i] for i in range(len(store_ends) - 1)]
        period = sp[len(sp) // 2 - 1]
    elif len(store_ends) >= 2:
        period = store_ends[1] - store_ends[0]
    return {"n": n, "nj": nj, "total": int(total) if total else None,
            "period": round(period, 1) if period else None,
            "load_store_overlap_cyc": [round(o, 1) for o in overlaps], "jobs": jobs}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=256)
    ap.add_argument("--nj", type=int, default=4)
    args = ap.parse_args()
    r = run(args.n, args.nj)
    print(f"\nload||store  N={r['n']}  NJOBS={r['nj']}  total={r['total']}  period={r['period']}")
    print(f"  load(N) || store(N-1) time-overlap (cyc): {r['load_store_overlap_cyc']}")
    p = r["period"]
    if p:
        print(f"  period vs max(read,write)={r['n']}  vs  read+write={2 * r['n']}")
        print(f"  -> {'OVERLAPS (~max, full-duplex used)' if p < 1.5 * r['n'] else 'SERIALIZES (~sum)'}")


if __name__ == "__main__":
    main()
