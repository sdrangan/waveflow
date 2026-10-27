"""The matched filter's golden: ``Y[i, j] = q_Y(conj(X[i]) * R[i, j])`` on stored integers, exact before
the one rounding."""

from __future__ import annotations

import numpy as np

from waveflow.utils import fixputils as fx
from waveflow.utils.fixputils import Format


def mf_golden(xr, xi, fx_: Format, rr, ri, fr: Format, fy: Format) -> tuple[np.ndarray, np.ndarray]:
    """``Y`` (``nsamp x nrx``) from ``X`` (``nsamp``) and ``R`` (``nsamp x nrx``), each a stored
    integer pair in its format."""
    xr, xi = np.asarray(xr)[:, None], np.asarray(xi)[:, None]
    p_rr, fp = fx.mult(xr, fx_, rr, fr)
    p_ii, _ = fx.mult(xi, fx_, ri, fr)
    p_ri, _ = fx.mult(xr, fx_, ri, fr)
    p_ir, _ = fx.mult(xi, fx_, rr, fr)
    re, f_re = fx.add(p_rr, fp, p_ii, fp)  # Re(conj(x) r) = xr rr + xi ri
    im, f_im = fx.sub(p_ri, fp, p_ir, fp)  # Im(conj(x) r) = xr ri - xi rr
    return fx.quantize(re, f_re, fy), fx.quantize(im, f_im, fy)
