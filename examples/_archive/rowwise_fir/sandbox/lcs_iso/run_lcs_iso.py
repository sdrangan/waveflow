"""run_lcs_iso.py — Rung 3: free-running load -> compute -> store, sweep the run-ahead DEPTH.

Inserts a trivial II=1 pass-through compute between load and store (Rung 2's load||store + a
middle stage) and sweeps the inter-stage FIFO depth. For N=256/job:
  depth < ~2*N  => load can't run a full job ahead of store -> period ~ sum (no overlap)
  depth >= ~2*N => load(N+1) overlaps store(N-1) -> period -> ~max (full-duplex restored)

If period drops toward ~max as depth grows, the FIR's lost full-duplex is a run-ahead/depth issue.
If it stays ~sum regardless, the 3-stage free-running DATAFLOW serializes structurally.

Run (project venv, Vitis 2025.1 + xsim)::

    PYTHONPATH=../../../.. ../../../../pysilicon-venv/Scripts/python.exe run_lcs_iso.py
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

TCL = HERE / "lcs_iso.tcl"
SOL = HERE / "lcs_iso_proj" / "solution1"
WORD_BYTES = 4


def _beats(b):
    return sum(1 for bt in b.get("beat_type", []) if bt == 0)


def _span(bursts, lo_w, hi_w):
    lo, hi = lo_w * WORD_BYTES, hi_w * WORD_BYTES
    sel = [b for b in bursts if lo <= int(b["addr"]) < hi]
    if not sel:
        return None
    t0 = min(float(b.get("data_tstart", b["tstart"])) for b in sel)
    t1 = max(float(b["data_tend"]) for b in sel)
    return t0, t1


def cosim(n, nj, depth):
    from vcdvcd import VCDVCD
    from waveflow.scripts.xsim_vcd import run_xsim_vcd
    from waveflow.utils.vcd import VcdParser

    env = {"WAVEFLOW_LCS_N": str(n), "WAVEFLOW_LCS_NJ": str(nj), "WAVEFLOW_LCS_DEPTH": str(depth)}
    last = ""
    for _ in (1, 2):
        res = toolchain.run_vitis_hls_result(TCL, work_dir=HERE, capture_output=True, env=env)
        last = (res.get("stdout") or "") + (res.get("stderr") or "")
        if "WAVEFLOW_SUCCESS" in last:
            break
    if "WAVEFLOW_SUCCESS" not in last:
        raise RuntimeError(f"depth {depth} cosim failed:\n{last[-2000:]}")
    total = CosimReportParser(sol_path=str(SOL), top="lcs_iso").get_transaction_cycles()

    vcd = run_xsim_vcd(top="lcs_iso", comp="lcs_iso_proj", out="dump.vcd", soln="solution1",
                       trace_level="port", workdir=HERE)
    vp = VcdParser(VCDVCD(str(vcd), signals=None, store_tvs=True))
    clk = vp.add_clock_signal()
    sigs, _ = vp.add_aximm_signals(prefix="m_axi_gmem_", dir="both", lite_only=False,
                                   short_name_prefix="gmem_")
    wb, rb, clk_ns = vp.extract_aximm_bursts(clk_name=clk, aximm_sigs=sigs)
    clk_ns = float(clk_ns)
    store_ends = []
    for j in range(nj):
        wr = _span(wb, 2 * j * n + n, 2 * j * n + 2 * n)
        if wr:
            store_ends.append(wr[1] / clk_ns)
    period = None
    if len(store_ends) >= 3:
        sp = [store_ends[i + 1] - store_ends[i] for i in range(len(store_ends) - 1)]
        period = sp[len(sp) // 2 - 1]
    return {"depth": depth, "total": int(total) if total else None,
            "period": round(period, 1) if period else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=256)
    ap.add_argument("--nj", type=int, default=6)
    ap.add_argument("--depths", type=int, nargs="+", default=[64, 256, 512, 1024, 2048])
    args = ap.parse_args()
    print(f"lcs (load->compute->store)  N={args.n}/job  NJOBS={args.nj}")
    print(f"  ideal max(read,write)={args.n}   serialized read+write={2 * args.n}\n")
    print(f"{'depth':>6} {'depth/N':>8} {'period':>8} {'cyc/word':>9}  verdict")
    for d in args.depths:
        r = cosim(args.n, args.nj, d)
        p = r["period"]
        verd = "-" if not p else ("OVERLAP (~max)" if p < 1.5 * args.n else "serialized (~sum)")
        cw = f"{p / args.n:.2f}" if p else "-"
        print(f"{d:>6} {d / args.n:>8.2f} {str(p):>8} {cw:>9}  {verd}", flush=True)


if __name__ == "__main__":
    main()
