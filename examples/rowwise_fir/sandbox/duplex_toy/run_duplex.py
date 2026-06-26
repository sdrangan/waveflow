"""run_duplex.py — cosim the duplex toy in all 3 modes; verdict full- vs half-duplex.

One Vitis m_axi bundle, II=1 loops of N beats:
  mode 0 read-only, mode 1 write-only, mode 2 read+write (copy).
Compares cosim transaction cycles:
  cosim(rw) ~= max(read, write)        -> FULL-duplex (occupancy MAXes)
  cosim(rw) ~= read + write            -> HALF-duplex (occupancy ADDS — the FIR model's assumption)

Run (project venv, Vitis 2025.1)::

    PYTHONPATH=../../../.. ../../../../pysilicon-venv/Scripts/python.exe run_duplex.py --n 1024
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

TCL = HERE / "duplex.tcl"
SOL = HERE / "duplex_proj" / "solution1"


def cosim(mode: int, n: int) -> int:
    env = {"WAVEFLOW_DUPLEX_MODE": str(mode), "WAVEFLOW_DUPLEX_N": str(n)}
    last = ""
    for _ in (1, 2):
        res = toolchain.run_vitis_hls_result(TCL, work_dir=HERE, capture_output=True, env=env)
        last = (res.get("stdout") or "") + (res.get("stderr") or "")
        if "WAVEFLOW_SUCCESS" in last:
            cyc = CosimReportParser(sol_path=str(SOL), top="duplex").get_transaction_cycles()
            if cyc is not None:
                return int(cyc)
    raise RuntimeError(f"mode {mode} cosim failed:\n{last[-2500:]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=1024)
    args = ap.parse_args()
    rd = cosim(0, args.n)
    print(f"  read-only  N={args.n}: {rd} cyc", flush=True)
    wr = cosim(1, args.n)
    print(f"  write-only N={args.n}: {wr} cyc", flush=True)
    rw = cosim(2, args.n)
    print(f"  read+write N={args.n}: {rw} cyc", flush=True)

    mx, sm = max(rd, wr), rd + wr
    full = abs(rw - mx) / mx
    half = abs(rw - sm) / sm
    verdict = "FULL-duplex (occupancy MAXes)" if full < half else "HALF-duplex (occupancy ADDS)"
    print(f"\n  read+write={rw}  vs  max(r,w)={mx} ({full*100:.0f}% off)  "
          f"r+w={sm} ({half*100:.0f}% off)")
    print(f"  VERDICT: {verdict}")


if __name__ == "__main__":
    main()
