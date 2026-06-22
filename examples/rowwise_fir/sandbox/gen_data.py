"""gen_data.py — numpy data + golden generator for the FIR sandbox.

Writes little-endian ``float32`` raw ``.bin`` fixtures (the convention
``csim_design -argv "$data_dir"`` consumes) plus ``meta.json``:

  X.bin         n_rows * n_cols           row-major input matrix
  h.bin         T                         FIR coefficients
  Y_golden.bin  n_rows * (n_cols - T + 1) bit-exact golden output
  meta.json     {n_rows, n_cols, T, dtype, layout}

The golden is the *valid* FIR per row,

    Y[i, j] = sum_{t=0..T-1} h[t] * X[i, j + (T-1) - t],   j in [0, n_cols - T]

accumulated **left-to-right in t** in float32 — the exact order ``fir_compute``
uses — so the C-simulation can compare bit-exactly (no tolerance).

Run with the project venv::

    ../pysilicon-venv/Scripts/python.exe examples/rowwise_fir/sandbox/gen_data.py
    # or for the sweep, import golden_valid_fir / write_fixture
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

# The ONE shared FIR golden (examples/rowwise_fir/fir_golden.py) — do not
# reimplement FIR here.  Importable whether run from the sandbox dir or the repo.
try:
    from examples.rowwise_fir.fir_golden import T, fir_golden
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from fir_golden import T, fir_golden

#: Back-compat alias — the golden used to live here as ``golden_valid_fir``.
golden_valid_fir = fir_golden


def write_fixture(out_dir: Path, n_rows: int, n_cols: int, seed: int = 0) -> dict:
    """Generate one fixture into ``out_dir`` and return its meta dict."""
    if n_cols > 1024:
        raise ValueError("n_cols exceeds NCOL_MAX=1024")
    if n_cols < T:
        raise ValueError(f"n_cols must be >= T={T}")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rng = np.random.default_rng(seed)
    # Small, well-conditioned values keep the float result reproducible and the
    # bit-exact check meaningful (not dominated by catastrophic cancellation).
    X = rng.standard_normal((n_rows, n_cols)).astype(np.float32)
    h = rng.standard_normal(T).astype(np.float32)
    Y = golden_valid_fir(X, h)

    # Little-endian float32 raw dumps.
    X.astype("<f4").tofile(out_dir / "X.bin")
    h.astype("<f4").tofile(out_dir / "h.bin")
    Y.astype("<f4").tofile(out_dir / "Y_golden.bin")

    meta = {
        "n_rows": int(n_rows),
        "n_cols": int(n_cols),
        "T": int(T),
        "out_len": int(n_cols - T + 1),
        "dtype": "float32",
        "byte_order": "little",
        "layout": "row_major",
        "seed": int(seed),
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    # Cosim sizes its m_axi memory model from the kernel's `depth=` pragma, which
    # must match the testbench array sizes exactly (oversized depth breaks the
    # cosim compare).  Emit the exact depths for run.tcl / the sweep to inject
    # via -D.
    x_depth = n_rows * n_cols
    y_depth = n_rows * (n_cols - T + 1)
    (out_dir / "dims.tcl").write_text(
        f"set WF_FIR_X_DEPTH {x_depth}\nset WF_FIR_Y_DEPTH {y_depth}\n", encoding="utf-8"
    )
    return meta


def main() -> None:
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description="Generate FIR sandbox fixtures.")
    ap.add_argument("--out", type=Path, default=here / "data")
    ap.add_argument("--n-rows", type=int, default=4)
    ap.add_argument("--n-cols", type=int, default=64)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()
    meta = write_fixture(args.out, args.n_rows, args.n_cols, args.seed)
    print(f"wrote fixture to {args.out}: {meta}")


if __name__ == "__main__":
    main()
