"""fir_golden.py — the ONE bit-exact FIR golden, shared by every FIR artifact.

Both the rowwise_fir accelerator (``examples/rowwise_fir/fir.py``) and the
hand-written HLS sandbox (``examples/rowwise_fir/sandbox/``) use this — there is a
single FIR definition, never a re-implementation.

Real-valued *valid* FIR per matrix row, accumulated **left-to-right in t** in
``float32`` — the exact order the hand-written kernel (the streaming shift-register
``compute``) uses — so the C-simulation compares bit-exactly (no tolerance):

    Y[i, j] = sum_{t=0..T-1} h[t] * X[i, j + (T-1) - t],   j in [0, n_cols - T]
"""
from __future__ import annotations

import numpy as np

#: FIR tap count (must match the kernel's compile-time ``T`` in fir_freerun_sandbox.hpp).
T = 8


def fir_golden(X: np.ndarray, h: np.ndarray) -> np.ndarray:
    """Bit-exact float32 valid FIR per row.

    ``X`` : ``(n_rows, n_cols)`` float32, ``h`` : ``(T,)`` float32
    ->     ``(n_rows, n_cols - T + 1)`` float32.

    The accumulation is an explicit left-to-right float32 reduction over ``t`` so
    the rounding sequence is identical to the C++ ``acc += h[t]*x[...]`` loop.
    """
    X = np.ascontiguousarray(X, dtype=np.float32)
    h = np.ascontiguousarray(h, dtype=np.float32)
    n_rows, n_cols = X.shape
    t = h.shape[0]
    out_len = n_cols - t + 1
    if out_len < 1:
        raise ValueError(f"n_cols={n_cols} too small for T={t} (valid FIR needs n_cols >= T)")
    Y = np.empty((n_rows, out_len), dtype=np.float32)
    for i in range(n_rows):
        for j in range(out_len):
            acc = np.float32(0.0)
            for k in range(t):
                acc = np.float32(acc + np.float32(h[k] * X[i, j + (t - 1) - k]))
            Y[i, j] = acc
    return Y
