"""run_fir_skel.py — Rung 4: REAL FIR compute in the controlled load/store skeleton.

Same skeleton as Rung 2/3 (which overlapped at every depth), but the middle stage is the actual
shift-register FIR (read n_cols/row, write n_cols-T+1/row, per-row window flush; taps constant, no
h-read). Measures the steady period from the Y write-burst spacing and compares to:

  max(read_words, write_words)  = the overlapped ideal (what Rung 3 achieved)
  read_words + write_words      = serialized (the real FIR's ~704 behavior)

If period ~ sum, the real compute breaks the overlap -> morph it feature by feature. If ~ max, the
break is elsewhere (e.g. the h-read or the generated FIFO depths).

Run (project venv, Vitis 2025.1 + xsim)::

    PYTHONPATH=../../../.. ../../../../pysilicon-venv/Scripts/python.exe run_fir_skel.py
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

T = 8
TCL = HERE / "fir_skel.tcl"
SOL = HERE / "fir_skel_proj" / "solution1"
WORD_BYTES = 4


def _end(bursts, lo_w, hi_w):
    lo, hi = lo_w * WORD_BYTES, hi_w * WORD_BYTES
    sel = [b for b in bursts if lo <= int(b["addr"]) < hi]
    return max((float(b["data_tend"]) for b in sel), default=None)


def run(nr, nc, nj, depth, buf=False):
    from vcdvcd import VCDVCD
    from waveflow.scripts.xsim_vcd import run_xsim_vcd
    from waveflow.utils.vcd import VcdParser

    env = {"WAVEFLOW_FS_NROW": str(nr), "WAVEFLOW_FS_NCOL": str(nc), "WAVEFLOW_FS_NJ": str(nj),
           "WAVEFLOW_FS_DEPTH": str(depth), "WAVEFLOW_FS_BUF": "1" if buf else "0"}
    last = ""
    for _ in (1, 2):
        res = toolchain.run_vitis_hls_result(TCL, work_dir=HERE, capture_output=True, env=env)
        last = (res.get("stdout") or "") + (res.get("stderr") or "")
        if "WAVEFLOW_SUCCESS" in last:
            break
    if "WAVEFLOW_SUCCESS" not in last:
        raise RuntimeError(f"{nr}x{nc} cosim failed:\n{last[-2000:]}")
    total = CosimReportParser(sol_path=str(SOL), top="fir_skel").get_transaction_cycles()

    vcd = run_xsim_vcd(top="fir_skel", comp="fir_skel_proj", out="dump.vcd", soln="solution1",
                       trace_level="port", workdir=HERE)
    vp = VcdParser(VCDVCD(str(vcd), signals=None, store_tvs=True))
    clk = vp.add_clock_signal()
    sigs, _ = vp.add_aximm_signals(prefix="m_axi_gmem_", dir="both", lite_only=False,
                                   short_name_prefix="gmem_")
    wb, _rb, clk_ns = vp.extract_aximm_bursts(clk_name=clk, aximm_sigs=sigs)
    clk_ns = float(clk_ns)
    outlen = nc - T + 1
    off, ends = 0, []
    for _j in range(nj):
        off += nr * nc                 # skip this job's X region
        yoff = off
        off += nr * outlen             # this job's Y region
        e = _end(wb, yoff, yoff + nr * outlen)
        if e is not None:
            ends.append(e / clk_ns)
    period = None
    if len(ends) >= 3:
        sp = [ends[i + 1] - ends[i] for i in range(len(ends) - 1)]
        period = sp[len(sp) // 2 - 1]
    read_w, write_w = nr * nc, nr * outlen
    return {"nr": nr, "nc": nc, "read": read_w, "write": write_w, "total": int(total) if total else None,
            "period": round(period, 1) if period else None,
            "maxrw": max(read_w, write_w), "sumrw": read_w + write_w}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nj", type=int, default=6)
    ap.add_argument("--depth", type=int, default=2048)
    ap.add_argument("--points", type=str, default="4x64,1x256")
    ap.add_argument("--buf", action="store_true", help="use the 2-pass buffered slice (framework path)")
    args = ap.parse_args()
    pts = [tuple(int(v) for v in p.split("x")) for p in args.points.split(",")]
    mode = "BUFFERED 2-pass (read/write_array_slice path)" if args.buf else "DIRECT gmem<->FIFO"
    print(f"fir_skel (REAL compute)  NJOBS={args.nj}  depth={args.depth}  m_axi={mode}\n")
    print(f"{'size':>8} {'period':>8} {'max(r,w)':>9} {'sum(r,w)':>9}  verdict")
    for nr, nc in pts:
        r = run(nr, nc, args.nj, args.depth, buf=args.buf)
        p = r["period"]
        if not p:
            print(f"{f'{nr}x{nc}':>8} {'None':>8}")
            continue
        verd = "OVERLAP (~max)" if p < 0.5 * (r["maxrw"] + r["sumrw"]) else "SERIALIZED (~sum)"
        print(f"{f'{nr}x{nc}':>8} {p:>8.0f} {r['maxrw']:>9} {r['sumrw']:>9}  {verd}", flush=True)


if __name__ == "__main__":
    main()
