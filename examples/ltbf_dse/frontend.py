"""frontend.py -- the radio unit's part: CP removal and the FFT of every antenna.

``plans/ltbf_dse/ltbf_dse.md`` step 2.1 (spec ``plans/ltbf_dse/ltbf_dse_spec.md`` §1).  It stands for
the O-RU of an O-RAN 7.2x split: the receiver's input is the frequency-domain grid.  Each symbol's
FFT window starts after its cyclic prefix, offset by the rendering's group delay
(:data:`examples.ltbf_dse.signals.GROUP_DELAY`) so that a UE's timing-advanced first path lands at
the window's start; the FFT is unitary, so unit-power time-domain noise is unit variance a resource
element.
"""

from __future__ import annotations

import numpy as np

from examples.ltbf_dse.signals import CP, GROUP_DELAY, N_FFT, SC_BINS, SYM_LEN


def ofdm_demodulate(y: np.ndarray, n_sym: int, start: int = GROUP_DELAY) -> np.ndarray:
    """``(N, n_sym, N_SC)``: the active subcarriers of *n_sym* symbols of the antennas' samples ``y``
    (``(N, L)``), symbol ``s``'s window at ``start + s * SYM_LEN + CP``."""
    n_ant = y.shape[0]
    out = np.empty((n_ant, n_sym, SC_BINS.size), dtype=np.complex128)
    for s in range(n_sym):
        w0 = start + s * SYM_LEN + CP
        win = y[:, w0 : w0 + N_FFT]
        if win.shape[1] != N_FFT:
            raise ValueError(
                f"symbol {s}: the window runs past the {y.shape[1]} samples rendered"
            )
        out[:, s, :] = (np.fft.fft(win, axis=1) / np.sqrt(N_FFT))[:, SC_BINS]
    return out
