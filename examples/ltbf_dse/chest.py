"""chest.py -- channel estimation in Python, every operation counted (spec §2).

``plans/ltbf_dse/ltbf_dse.md`` step 2.2.  Two estimators, both through an
:class:`~examples.ltbf_dse.opcount.OpLog` so their complexity can be read off later:

* :func:`srs_blocks` -- per sector and comb, ``E_c = Y_c ⊙ conj(r)`` then ``Ĥ_c = E_c · P``: ``P``
  holds each port's linear filter (de-rotate, IDFT, its 24-tap window with a 4-tap lead and a 4-tap
  raised-cosine taper, DFT, its phase ramp removed, the SRS amplitude divided out) evaluated at the 8
  snapshot subcarriers (every 24th of the comb);
* :func:`dmrs_weights` -- per UE, on its whitened projection's DMRS REs: despread the OCC over each
  2 x 2 block, remove the mean delay, interpolate linearly across frequency (nearest at the edges),
  restore the delay, and the MMSE weight ``w = h / (1 + |h|^2)``.

:func:`long_term_from_srs` turns the SRS estimates into the :class:`ltbf_link.LongTerm` the copied
long-term code (``ltbf.py``) solves.
"""

from __future__ import annotations

from functools import cache

import numpy as np

from examples.ltbf_dse import ltbf_link as lk
from examples.ltbf_dse import signals as sg
from examples.ltbf_dse.opcount import OpLog

#: The SRS window: 24 taps a port, lead 4, raised-cosine taper 4 (spec §2, spike f).
SRS_LEAD = 4
SRS_TAPER = 4
#: The 8 snapshot subcarriers, as indices within a comb's 192.
SNAP = 12 + 24 * np.arange(lk.N_SRS)


def srs_window(
    m: int = sg.M_SRS, lead: int = SRS_LEAD, taper: int = SRS_TAPER
) -> np.ndarray:
    """Port 0's delay window over ``m`` taps (cyclic), ``m / 8`` taps from ``-lead``."""
    w_len = m // sg.N_CS
    shape = np.ones(w_len)
    if taper:
        ramp = 0.5 * (1 - np.cos(np.pi * (np.arange(taper) + 0.5) / taper))
        shape[:taper] = ramp
        shape[-taper:] = ramp[::-1]
    w = np.zeros(m)
    w[(np.arange(w_len) - lead) % m] = shape
    return w


@cache
def srs_filter(port: int) -> np.ndarray:
    """``(192, 8)``: port *port*'s estimate at the snapshot subcarriers as a linear map of the
    de-rotated comb ``E``.  Its cyclic shift puts it at delay ``-24·port`` (mod 192)."""
    m = sg.M_SRS
    finv = np.fft.ifft(np.eye(m), axis=0)  # column j: ifft of unit vector j
    w = np.roll(srs_window(), -port * (m // sg.N_CS))
    k = np.fft.fft(w[:, None] * finv, axis=0)
    k *= np.exp(-2j * np.pi * port * np.arange(m) / sg.N_CS)[:, None] / sg.SRS_AMP
    return k[SNAP, :].T.copy()


def srs_blocks(y_srs: np.ndarray, ues: np.ndarray, log: OpLog) -> dict[int, np.ndarray]:
    """Per UE in *ues*: its ``(N, 8)`` SRS block from one sector's SRS symbol ``y_srs`` (``(N, 792)``)."""
    base = sg.zc_base()
    out: dict[int, np.ndarray] = {}
    log.stage = "srs"
    for comb in range(sg.K_TC):
        on = [int(u) for u in ues if sg.srs_assignment(int(u))[0] == comb]
        if not on:
            continue
        yc = y_srs[:, sg.srs_subcarriers(comb)]
        e = log.mul(yc, base[None, :], conj_b=True)
        p = np.hstack([srs_filter(sg.srs_assignment(u)[1]) for u in on])
        h = log.matmul(e, p)
        for k, u in enumerate(on):
            out[u] = h[:, k * lk.N_SRS : (k + 1) * lk.N_SRS]
    return out


def long_term_from_srs(
    blocks_per_sector: list[dict[int, np.ndarray]], pc: lk.PowerControl, log: OpLog
) -> lk.LongTerm:
    """The long-term inputs from the SRS estimates (``ltbf_link.long_term``'s structure, whose
    estimates are drawn from the true channel): ``Ĥα`` over every connected UE per sector.
    """
    log.stage = "h_alpha"
    h_alpha, rhs, own, srs_own = [], [], [], {}
    for s, blocks in enumerate(blocks_per_sector):
        cols = [blocks[int(u)] for u in pc.iconn]
        n = next(iter(blocks.values())).shape[0]
        ha = np.hstack(cols) / np.sqrt(lk.N_SRS) if cols else np.zeros((n, 0), complex)
        h_alpha.append(log.copy(ha, what="h_alpha"))
        mine = np.array([u for u in pc.iconn if pc.gnb_idx[u] == s], dtype=int)
        own.append(mine)
        rhs.append(
            np.hstack([blocks[int(u)] for u in mine]) if mine.size else np.zeros((n, 0))
        )
        for u in mine:
            srs_own[int(u)] = blocks[int(u)]
    return lk.LongTerm(h_alpha, rhs, own, srs_own)


def dmrs_weights(
    z_dmrs: np.ndarray, port: int, sector: int, log: OpLog
) -> tuple[np.ndarray, np.ndarray]:
    """``(h, w)``, each ``(r, 792)``: a UE's effective channel and MMSE weight from its whitened
    projection on the two DMRS symbols, ``z_dmrs`` (``(r, 2, 792)``)."""
    log.stage = "dmrs"
    r = z_dmrs.shape[0]
    n, occ = sg.dmrs_res(port)
    seq = sg.dmrs_sequence(sector)
    nb = n.size // 2
    # Despread: the 4 REs of each block (2 subcarriers x 2 symbols) times conj(DMRS) and the OCC.
    ref = (occ * sg.DMRS_AMP * seq[:, n]).conj()  # (2, 2 nb)
    prod = log.mul(z_dmrs[:, :, n], ref[None])  # (r, 2, 2 nb): 4r cmacs a block
    hb = prod.reshape(r, 2, nb, 2).sum(axis=(1, 3)) / (4 * sg.DMRS_AMP**2)
    fb = 6.0 * np.arange(nb) + 2 * sg.dmrs_port_ref(port)[0] + 0.5
    # The mean delay: the phase slope between neighbouring blocks (6 subcarriers apart).
    slope = np.angle(np.sum(hb[:, 1:] * hb[:, :-1].conj())) / 6.0
    hb = log.mul(hb, np.exp(-1j * slope * fb)[None])
    f = np.arange(sg.N_SC, dtype=float)
    h = np.empty((r, sg.N_SC), dtype=np.complex128)
    for k in range(r):
        h[k] = np.interp(f, fb, hb[k].real) + 1j * np.interp(f, fb, hb[k].imag)
    log.note("interp", (hb.shape, sg.N_SC), h, cmacs=2 * r * sg.N_SC, n_in=hb.size)
    h = log.mul(h, np.exp(1j * slope * f)[None])
    den = log.add(log.sumsq(h, axis=0), 1.0)
    w = log.divide(h, den[None, :])
    return h, w
