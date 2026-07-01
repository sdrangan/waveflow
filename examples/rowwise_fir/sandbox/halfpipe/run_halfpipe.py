"""run_halfpipe.py — half-pipe decomposition: load+compute (read-only) vs compute+store (write-only).

Each mode uses only ONE m_axi direction, so there's no read+write contention — it isolates each
direction's interaction with the compute middle stage:
  lc = real m_axi LOAD  + compute + fake store  -> period from READ-burst spacing
  cs = fake BRAM load   + compute + real STORE  -> period from WRITE-burst spacing

If both ~= n (1 cyc/sample), each direction+compute is clean and the FIR slowdown is a 3-way
read+write+compute effect; if one is ~2n, that direction+compute is the culprit.

Run (project venv, Vitis 2025.1 + xsim)::

    PYTHONPATH=../../../.. ../../../../pysilicon-venv/Scripts/python.exe run_halfpipe.py --n 256 --nj 6
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

TCL = HERE / "halfpipe.tcl"
WORD_BYTES = 4


def _beats(b):
    return sum(1 for bt in b.get("beat_type", []) if bt == 0)


def _span(bursts, lo_w, hi_w):
    lo, hi = lo_w * WORD_BYTES, hi_w * WORD_BYTES
    sel = [b for b in bursts if lo <= int(b["addr"]) < hi]
    if not sel:
        return None
    return max(float(b["data_tend"]) for b in sel)


def run(mode: str, n: int, nj: int, depth: int) -> dict:
    from vcdvcd import VCDVCD
    from waveflow.scripts.xsim_vcd import run_xsim_vcd
    from waveflow.utils.vcd import VcdParser

    proj = f"{mode}_iso_proj"
    sol = HERE / proj / "solution1"
    env = {"WAVEFLOW_HP_MODE": mode, "WAVEFLOW_HP_N": str(n), "WAVEFLOW_HP_NJ": str(nj),
           "WAVEFLOW_HP_DEPTH": str(depth)}
    last = ""
    for _ in (1, 2):
        res = toolchain.run_vitis_hls_result(TCL, work_dir=HERE, capture_output=True, env=env)
        last = (res.get("stdout") or "") + (res.get("stderr") or "")
        if "WAVEFLOW_SUCCESS" in last:
            break
    if "WAVEFLOW_SUCCESS" not in last:
        raise RuntimeError(f"{mode} cosim failed:\n{last[-2000:]}")
    total = CosimReportParser(sol_path=str(sol), top=f"{mode}_iso").get_transaction_cycles()

    vcd = run_xsim_vcd(top=f"{mode}_iso", comp=proj, out="dump.vcd", soln="solution1",
                       trace_level="port", workdir=HERE)
    vp = VcdParser(VCDVCD(str(vcd), signals=None, store_tvs=True))
    clk = vp.add_clock_signal()
    sigs, _ = vp.add_aximm_signals(prefix="m_axi_gmem_", dir="both", lite_only=False,
                                   short_name_prefix="gmem_")
    wb, rb, clk_ns = vp.extract_aximm_bursts(clk_name=clk, aximm_sigs=sigs)
    clk_ns = float(clk_ns)
    bursts = rb if mode == "lc" else wb     # lc reads X at j*n; cs writes Y at j*n
    ends = [e / clk_ns for j in range(nj) if (e := _span(bursts, j * n, j * n + n)) is not None]
    period = None
    if len(ends) >= 3:
        sp = [ends[i + 1] - ends[i] for i in range(len(ends) - 1)]
        period = sp[len(sp) // 2 - 1]
    return {"mode": mode, "total": int(total) if total else None,
            "period": round(period, 1) if period else None}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=256)
    ap.add_argument("--nj", type=int, default=6)
    ap.add_argument("--depth", type=int, default=1024)
    args = ap.parse_args()
    print(f"half-pipe  N={args.n}/job  NJOBS={args.nj}  depth={args.depth}")
    print(f"  ideal per-job ~= n = {args.n}  (one m_axi direction + compute, II=1)\n")
    print(f"{'mode':>6} {'period':>8} {'cyc/sample':>11}  meaning")
    for mode, desc in (("lc", "real LOAD + compute (read-only)"),
                       ("cs", "compute + real STORE (write-only)")):
        r = run(mode, args.n, args.nj, args.depth)
        p = r["period"]
        cw = f"{p / args.n:.2f}" if p else "-"
        print(f"{mode:>6} {str(p):>8} {cw:>11}  {desc}", flush=True)


if __name__ == "__main__":
    main()
