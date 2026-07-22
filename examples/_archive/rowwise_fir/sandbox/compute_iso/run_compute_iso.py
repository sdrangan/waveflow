"""run_compute_iso.py — Rung 1: cosim the isolated FIR compute over a (nrow,ncol) grid; fit
compute_time = L0 + L1*nrow + II*(nrow*ncol).

No load/store/m_axi — the cosim transaction latency IS the compute time. The fit's II is the true
per-sample compute interval (target 1); L1 is the per-row window-flush cost (target small); L0 the
fixed FP-pipeline fill + cmd/resp handshake.

Run (project venv, Vitis 2025.1)::

    PYTHONPATH=../../../.. ../../../../pysilicon-venv/Scripts/python.exe run_compute_iso.py
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[4]
sys.path.insert(0, str(REPO))

from waveflow.toolchain import toolchain                  # noqa: E402
from waveflow.utils.cosimparse import CosimReportParser   # noqa: E402

TCL = HERE / "compute_iso.tcl"
SOL = HERE / "compute_iso_proj" / "solution1"
GRID = [(1, 16), (1, 32), (1, 64), (1, 128), (1, 256),
        (4, 16), (4, 32), (4, 64), (4, 128), (4, 256)]
HOLDOUT = (2, 96)


def cosim(nrow: int, ncol: int) -> int:
    env = {"WAVEFLOW_CI_NROW": str(nrow), "WAVEFLOW_CI_NCOL": str(ncol)}
    last = ""
    for _ in (1, 2):
        res = toolchain.run_vitis_hls_result(TCL, work_dir=HERE, capture_output=True, env=env)
        last = (res.get("stdout") or "") + (res.get("stderr") or "")
        if "WAVEFLOW_SUCCESS" in last:
            cyc = CosimReportParser(sol_path=str(SOL), top="compute_iso").get_transaction_cycles()
            if cyc is not None:
                return int(cyc)
    raise RuntimeError(f"compute_iso {nrow}x{ncol} cosim failed:\n{last[-2500:]}")


def main() -> None:
    pts = []
    for nr, nc in GRID + [HOLDOUT]:
        cyc = cosim(nr, nc)
        pts.append((nr, nc, nr * nc, cyc, (nr, nc) == HOLDOUT))
        print(f"  {nr}x{nc}: {cyc} cyc  ({cyc / (nr * nc):.2f}/sample)", flush=True)

    tr = [p for p in pts if not p[4]]
    A = np.array([[1, p[0], p[2]] for p in tr], dtype=float)
    y = np.array([p[3] for p in tr], dtype=float)
    (L0, L1, II), *_ = np.linalg.lstsq(A, y, rcond=None)
    pred = A @ np.array([L0, L1, II])
    print("\nFIT compute_time = L0 + L1*nrow + II*(nrow*ncol)")
    print(f"  L0={L0:.1f}  L1(per-row)={L1:.2f}  II(per-sample)={II:.3f}")
    print(f"  train worst rel_err = {np.max(np.abs(pred - y) / y) * 100:.1f}%")
    for p in pts:
        if p[4]:
            pr = L0 + L1 * p[0] + II * p[2]
            print(f"  HOLDOUT {p[0]}x{p[1]}: pred={pr:.0f} actual={p[3]} "
                  f"rel_err={abs(pr - p[3]) / p[3] * 100:.1f}%")
    print(f"\n  VERDICT: compute II = {II:.2f} "
          f"({'~1, compute pipelines cleanly' if II < 1.4 else 'NOT 1 — compute is the problem'})")


if __name__ == "__main__":
    main()
