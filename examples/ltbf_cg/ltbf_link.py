"""ltbf_link.py -- from a traced drop to the inputs of the long-term solve.

``plans/mimo_cg/ltbf_cg_dse.md`` step 1.1.  The model is the LTBF paper's code, read for its
semantics (``salsa-ltbf/salsa/salsa/ray_tracing.py`` ``control_ue_power``, ``compute_mimo_matrix``;
``salsa_cap.py`` ``draw_srs``, ``estimate_Q``, ``compute_lt_bf``):

* **channels.** Each sector's array is applied to the one-element trace of :mod:`scenario` the way
  Sionna's synthetic array does, then the frequency response is taken on a band's grid;
* **bands.** ``"1.44MHz"``: the code's grid, ``n_rb`` = 69 points over ``scs * n_sc_rb``
  (the paper's deviation D21); ``"100MHz"``: 66 equally spaced samples spanning the 792 active
  subcarriers at 120 kHz edge to edge (1.46 MHz apart), the band the paper's text states.  The noise floor stays the code's
  (``used_bw`` = 192 subcarriers x 120 kHz) in both, so a band changes only the channel's
  frequency diversity;
* **power control.** Each UE is served by its strongest sector; a UE whose SNR at full power is
  below the target range is in outage; the rest are scaled so the SNR summed over the array lands at
  ``min(SNR at full power, upper target)``, with the noise normalised to unit variance;
* **SRS.** ``N_SRS`` = 8 distinct random frequency samples per UE, the same for every sector, with
  independent estimation noise per sector at ``G_SRS`` (``None``: noiseless);
* **long-term statistics.** Per sector, ``Q = I + sum_j Q_j`` over **every connected UE**
  (``Q_j = H_j H_j^H / N_SRS`` from that sector's SRS of UE j), so ``H_alpha`` stacks
  ``H_j / sqrt(N_SRS)`` over the connected UEs (``alpha_j`` is already in the power-controlled
  channel: never scale twice).  The right-hand sides of a sector are the SRS blocks of its own UEs.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache

import numpy as np

from examples.ltbf_cg import scenario as sc

#: The paper's link constants (``ray_tracing.py`` defaults and ``salsa_main.py``).
SCS = 120e3
N_SC_RB = 12
N_RB = 69
#: ``used_bw``: the bandwidth the noise floor is computed over (``ray_tracing.py:245-251``).
USED_BW = 64 * 3 * SCS
NOISE_FIGURE_DB = 2.0
PTX_MAX_DBM = 26.0
NOISE_FLOOR_DBM = -174.0 + 10.0 * np.log10(USED_BW) + NOISE_FIGURE_DB
N_SRS = 8
#: The paper's nominal SRS processing gain (``salsa_main.py:85``).
G_SRS_DB = 10.0
#: Array sizes (rows = columns).
ARRAYS = {"8x8": 8, "16x16": 16, "32x32": 32}
#: SINR target ranges, dB.
TARGETS = {"-6_3": (-6.0, 3.0), "-6_14": (-6.0, 14.0)}


def band_frequencies(band: str) -> np.ndarray:
    """Baseband frequency samples [Hz] of a band."""
    if band == "1.44MHz":
        return np.linspace(-0.5, 0.5, N_RB) * (SCS * N_SC_RB)
    if band == "100MHz":
        return np.linspace(-0.5, 0.5, 66) * (792 * SCS)
    raise ValueError(f"unknown band {band!r}")


@cache
def element_positions(n: int, yaw: float) -> np.ndarray:
    """(3, n*n) element positions [m] of an n x n tr38901 array rotated by ``yaw``: Sionna's own
    ``AntennaArray.rotate``, so the element order and geometry are Sionna's."""
    rt, mi = sc.import_sionna()
    arr = rt.PlanarArray(
        num_rows=n,
        num_cols=n,
        vertical_spacing=0.5,
        horizontal_spacing=0.5,
        pattern="tr38901",
        polarization="V",
    )
    p = arr.rotate(sc.WAVELENGTH, mi.Point3f(float(yaw), 0.0, 0.0))
    return np.array([np.array(p.x), np.array(p.y), np.array(p.z)], dtype=np.float64)


def channel(drop: dict, snapshot: int, n: int, band: str) -> np.ndarray:
    """``H[s, ant, u, f]``: sector ``s``'s ``n x n`` array, UE ``u``, frequency sample ``f``.

    Each path's coefficient times ``exp(j 2 pi / lambda * r_ant . k_rx)`` (the synthetic array),
    summed with ``exp(-j 2 pi f tau)``.  Absolute delays: a per-link delay offset multiplies a UE's
    whole response by one phase per frequency, which no covariance or SINR sees.
    """
    a = drop[f"a{snapshot}"] * drop[f"valid{snapshot}"]
    tau = np.where(drop[f"valid{snapshot}"], drop[f"tau{snapshot}"], 0.0)
    k = drop[f"k_rx{snapshot}"]
    f = band_frequencies(band)
    n_sect, n_ue, _ = a.shape
    out = np.empty((n_sect, n * n, n_ue, f.size), dtype=np.complex128)
    for s in range(n_sect):
        r = element_positions(n, float(drop["yaws"][s]))  # (3, N)
        phase = np.einsum("dn,upd->nup", r, k[s]) * (2.0 * np.pi / sc.WAVELENGTH)
        ant = a[s][None] * np.exp(1j * phase)  # (N, U, P)
        rot = np.exp(-2j * np.pi * tau[s][:, :, None] * f[None, None, :])  # (U, P, F)
        out[s] = np.einsum("nup,upf->nuf", ant, rot)
    return out


@dataclass(frozen=True)
class PowerControl:
    """``control_ue_power`` of the paper: serving sector, connected UEs, channel scale."""

    gnb_idx: np.ndarray  # (U,), -1 in outage
    iconn: np.ndarray  # connected UEs
    scale: np.ndarray  # (U,), 0 in outage


def power_control(h: np.ndarray, target: tuple[float, float]) -> PowerControl:
    """Per-UE serving sector, outage and scale, from the unscaled channel ``h[s, ant, u, f]``."""
    gain = (np.abs(h) ** 2).mean(axis=3).sum(axis=1)  # (S, U)
    gmax = np.maximum(gain.max(axis=0), 1e-30)
    gnb = gain.argmax(axis=0)
    snr_max = PTX_MAX_DBM + 10.0 * np.log10(gmax) - NOISE_FLOOR_DBM
    conn = np.where(snr_max >= target[0])[0]
    gnb = np.where(snr_max >= target[0], gnb, -1)
    htgt = 10.0 ** (0.1 * np.minimum(snr_max, target[1]))
    scale = np.zeros(h.shape[2])
    scale[conn] = np.sqrt(htgt[conn] / gmax[conn])
    return PowerControl(gnb, conn, scale)


def scaled(h: np.ndarray, pc: PowerControl) -> np.ndarray:
    """The power-controlled channel, data noise normalised to unit variance."""
    return h * pc.scale[None, None, :, None]


@dataclass(frozen=True)
class LongTerm:
    """The long-term inputs of every sector: ``H_alpha`` and the right-hand sides of its UEs."""

    h_alpha: list[np.ndarray]  # per sector (N, N_SRS * |conn|)
    rhs: list[
        np.ndarray
    ]  # per sector (N, N_SRS * |own UEs|): the SRS blocks of its own UEs
    own: list[
        np.ndarray
    ]  # per sector: its connected UEs, in the order of rhs's column blocks
    srs_own: dict[int, np.ndarray]  # UE -> its serving sector's SRS block (N, N_SRS)


def long_term(
    h: np.ndarray,
    pc: PowerControl,
    rng: np.random.Generator,
    g_srs_db: float | None = G_SRS_DB,
) -> LongTerm:
    """The SRS draw and the per-sector long-term inputs of ``compute_lt_bf`` (canonical form)."""
    n_sect, n_ant, _, n_f = h.shape
    sigma2 = None if g_srs_db is None else 10.0 ** (-0.1 * g_srs_db)
    blocks: dict[tuple[int, int], np.ndarray] = {}
    for u in pc.iconn:
        idx = rng.choice(n_f, N_SRS, replace=False)
        for s in range(n_sect):
            hs = h[s, :, u, :][:, idx]
            if sigma2 is not None:
                hs = hs + np.sqrt(sigma2 / 2.0) * (
                    rng.standard_normal(hs.shape) + 1j * rng.standard_normal(hs.shape)
                )
            blocks[(s, int(u))] = hs
    h_alpha, rhs, own, srs_own = [], [], [], {}
    for s in range(n_sect):
        cols = [blocks[(s, int(u))] for u in pc.iconn]
        h_alpha.append(
            np.hstack(cols) / np.sqrt(N_SRS) if cols else np.zeros((n_ant, 0), complex)
        )
        mine = np.array([u for u in pc.iconn if pc.gnb_idx[u] == s], dtype=int)
        own.append(mine)
        rhs.append(
            np.hstack([blocks[(s, int(u))] for u in mine])
            if mine.size
            else np.zeros((n_ant, 0), complex)
        )
        for u in mine:
            srs_own[int(u)] = blocks[(s, int(u))]
    return LongTerm(h_alpha, rhs, own, srs_own)


def q_hat(h_alpha: np.ndarray) -> np.ndarray:
    """``Q = I + H_alpha H_alpha^H`` (explicit form)."""
    return np.eye(h_alpha.shape[0]) + h_alpha @ h_alpha.conj().T
