"""signals.py -- the received signals of a drop: OFDM waveforms through the ray-traced channels.

``plans/ltbf_dse/ltbf_dse.md`` step 2.1 (spec ``plans/ltbf_dse/ltbf_dse_spec.md`` §1).  What a sector's
array receives from every UE, rendered from a drop's paths (``scenario.load_drop``):

* **the channel** is the synthetic array of :func:`examples.ltbf_dse.ltbf_link.channel`: one delay per
  (UE, path), shared by the sector's antennas, the array's phase in the amplitude; each UE scaled by
  its power control (``ltbf_link.power_control``) and **timing-advanced** to its serving sector's
  earliest path; Doppler zero inside a snapshot;
* **rendering** is ``waveflow.rfenv``'s, factored: a sector's paths share their delays across its
  antennas, so the received samples are ``A @ D`` -- ``D`` each (UE, path)'s waveform delayed once
  with rfenv's own interpolator (:func:`waveflow.rfenv.apply.fractional_delay_kernel`), ``A``
  (antennas x (UEs * paths)) the coefficients.  It equals :func:`waveflow.rfenv.apply.reference_apply`
  to rounding (spike 1.2 a: 4e-16) at about 1/130 of :class:`~waveflow.rfenv.apply.ChannelApplier`'s
  time, which makes rendering inside every sweep job affordable;
* **noise** is :class:`waveflow.rfenv.noise.ThermalNoise` of unit power a sample, drawn by sample
  index: with the unitary FFT of :mod:`examples.ltbf_dse.frontend` it is unit variance a resource
  element, the normalization of the power-controlled channel;
* **OFDM** (:func:`ofdm_modulate`): 120 kHz, a unitary 1024-point IFFT at 122.88 Msps, CP 72, the 792
  active subcarriers centred on DC.

Every random draw is seeded from ``(drop, snapshot, sector)``: rendering twice gives the same bytes.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from examples.ltbf_dse import ltbf_link as lk
from examples.ltbf_dse import scenario as sc
from waveflow.rfenv.apply import (
    DEFAULT_BETA,
    DEFAULT_FILT_LEN,
    fractional_delay_kernel,
    split_delay,
)
from waveflow.rfenv.noise import ThermalNoise
from waveflow.rfenv.paths import (
    ABSOLUTE_DELAYS,
    SCHEMA_VERSION,
    PathSet,
    channel_map,
    spec_hash,
)

#: Sample rate, FFT size, cyclic prefix, active subcarriers, symbols a slot.
FS = 122.88e6
N_FFT = 1024
CP = 72
N_SC = 792
N_SYM = 14
SYM_LEN = N_FFT + CP
#: The interpolator's declared group delay (rfenv: ``filt_len / 2``), in samples.
GROUP_DELAY = DEFAULT_FILT_LEN // 2
#: FFT bin of each active subcarrier, lowest frequency first: ``k = -396 .. 395``.
SC_BINS = np.arange(-N_SC // 2, N_SC // 2) % N_FFT


@dataclass(frozen=True)
class SectorPaths:
    """One sector's view of every UE in one snapshot, ready to render."""

    a: (
        np.ndarray
    )  # (N, U, P) complex: path coefficient x array phase x power-control scale
    tau: np.ndarray  # (U, P) seconds after the UE's timing-advanced start (>= 0)
    valid: np.ndarray  # (U, P) bool


def timing_advance(drop: dict, snapshot: int, gnb: np.ndarray) -> np.ndarray:
    """``(U,)``: each UE's earliest valid path at its serving sector (in outage, at a sector that hears
    it; 0 for a UE no sector hears)."""
    tau, valid = drop[f"tau{snapshot}"], drop[f"valid{snapshot}"]
    ta = np.zeros(tau.shape[1])
    for u in range(tau.shape[1]):
        heard = valid[:, u].any(axis=1)
        if not heard.any():
            continue
        s = int(gnb[u]) if gnb[u] >= 0 else int(np.argmax(heard))
        ta[u] = tau[s, u][valid[s, u]].min()
    return ta


def sector_paths(
    drop: dict, snapshot: int, sector: int, n: int, pc: lk.PowerControl, ta: np.ndarray
) -> SectorPaths:
    """Sector *sector*'s ``n x n`` array: the paths of every UE, scaled by its power control (a UE in
    outage transmits nothing) and delayed relative to its timing advance."""
    a = drop[f"a{snapshot}"][sector] * drop[f"valid{snapshot}"][sector]
    k = drop[f"k_rx{snapshot}"][sector]
    r = lk.element_positions(n, float(drop["yaws"][sector]))
    phase = np.einsum("dn,upd->nup", r, k) * (2.0 * np.pi / sc.WAVELENGTH)
    coef = a[None] * np.exp(1j * phase) * pc.scale[None, :, None]
    tau = drop[f"tau{snapshot}"][sector] - ta[:, None]
    valid = drop[f"valid{snapshot}"][sector] & (pc.scale[:, None] > 0)
    # Co-located sectors: no valid path of a heard UE arrives before its timing advance (spike a).
    tau = np.where(valid, np.maximum(tau, 0.0), 0.0)
    return SectorPaths(coef, tau, valid)


def delayed(x: np.ndarray, tau: np.ndarray, valid: np.ndarray) -> np.ndarray:
    """``(U*P, L)``: each (UE, path)'s waveform ``x[u]`` delayed by ``tau[u, p]`` plus the group delay
    ``GROUP_DELAY``, with rfenv's interpolator -- what :class:`~waveflow.rfenv.apply.ChannelApplier`
    applies to every antenna of the path."""
    half = DEFAULT_FILT_LEN // 2
    u_n, p_n = tau.shape
    length = x.shape[1]
    out = np.zeros((u_n * p_n, length), dtype=np.complex128)
    for u in range(u_n):
        for p in range(p_n):
            if not valid[u, p]:
                continue
            k, mu = split_delay(float(tau[u, p]), FS, half)
            c = fractional_delay_kernel(mu, DEFAULT_FILT_LEN, DEFAULT_BETA)
            # rfenv: y(n) = sum_i c[i] x(n - k + half - 1 - i) = convolve(x, c)[n - k + half - 1].
            full = np.convolve(x[u], c)
            idx = np.arange(length) - k + half - 1
            ok = (idx >= 0) & (idx < full.size)
            out[u * p_n + p, ok] = full[idx[ok]]
    return out


def render(paths: SectorPaths, x: np.ndarray) -> np.ndarray:
    """``(N, L)``: the sector's antennas, noiseless, for UE waveforms ``x`` (``(U, L)``)."""
    n, u, p = paths.a.shape
    return paths.a.reshape(n, u * p) @ delayed(x, paths.tau, paths.valid)


def noise(n_ant: int, length: int, drop: int, snapshot: int, sector: int) -> np.ndarray:
    """``(N, L)`` unit-power complex noise, the same for the same (drop, snapshot, sector)."""
    return ThermalNoise(
        1.0, seed=int(drop), tag=10 * int(snapshot) + int(sector)
    ).samples(n_ant, 0, length)


def pathset(paths: SectorPaths) -> PathSet:
    """The same channel as an rfenv :class:`PathSet` (for checking :func:`render` against rfenv)."""
    n, u, p = paths.a.shape
    spec = {"kind": "examples.ltbf_dse.signals"}
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "carrier_hz": sc.FC,
        "delays": ABSOLUTE_DELAYS,
        "channels": channel_map(
            [("bs", i) for i in range(n)], [("ue", j) for j in range(u)]
        ),
        "generator": {"name": "examples.ltbf_dse.signals"},
        "spec": spec,
        "spec_hash": spec_hash(spec),
        "created": "2026-10-28",
    }
    tau = np.broadcast_to(paths.tau, (n, u, p))
    valid = np.broadcast_to(paths.valid, (n, u, p))
    return PathSet(paths.a, tau, np.zeros((n, u, p)), valid, manifest)


def ofdm_modulate(grid: np.ndarray) -> np.ndarray:
    """``(U, S * SYM_LEN)``: each UE's ``(S, N_SC)`` resource grid as samples -- the active
    subcarriers into their bins, a unitary IFFT, the CP in front of each symbol."""
    u_n, s_n, _ = grid.shape
    freq = np.zeros((u_n, s_n, N_FFT), dtype=np.complex128)
    freq[:, :, SC_BINS] = grid
    t = np.fft.ifft(freq, axis=2) * np.sqrt(N_FFT)
    return np.concatenate([t[:, :, -CP:], t], axis=2).reshape(u_n, s_n * SYM_LEN)


def frequency_response(paths: SectorPaths) -> np.ndarray:
    """``(N, U, N_SC)``: the channel the front end sees on each active subcarrier, for an FFT window
    that starts ``GROUP_DELAY`` samples after a UE's timing-advanced symbol start -- the ideal (no
    interpolation error) reference."""
    f = (SC_BINS.astype(float) - np.where(SC_BINS >= N_FFT // 2, N_FFT, 0)) * (
        FS / N_FFT
    )
    rot = (
        np.exp(-2j * np.pi * paths.tau[:, :, None] * f[None, None, :])
        * paths.valid[:, :, None]
    )
    return np.einsum("nup,upf->nuf", paths.a, rot)


# -- the spec's waveforms (spec §1) ------------------------------------------------------------------

#: SRS: 64 RBs sounded, centred in the 66 (subcarriers 12 .. 779), comb 4, 8 cyclic shifts a comb.
SRS_OFFSET = 12
K_TC = 4
N_CS = 8
M_SRS = 192
#: Per-RE amplitude of the SRS (the UE's power over its 192 comb REs: 4x a data RE's energy).
SRS_AMP = 2.0
#: PUSCH: DMRS on symbols 2 and 3 (type 2, double symbol), data on the others.
DMRS_SYMS = (2, 3)
DATA_SYMS = tuple(s for s in range(N_SYM) if s not in DMRS_SYMS)
#: DMRS amplitude: 4.77 dB above a data RE (3 CDM groups, no data; spec gate D).
DMRS_AMP = float(np.sqrt(3.0))


def zc_base(m: int = M_SRS) -> np.ndarray:
    """The SRS base sequence: Zadoff-Chu of the largest prime length below *m* (191), root 1,
    extended cyclically to *m*."""
    p = max(q for q in range(2, m + 1) if all(q % d for d in range(2, int(q**0.5) + 1)))
    k = np.arange(m) % p
    return np.exp(-1j * np.pi * k * (k + 1) / p)


def srs_assignment(u: int) -> tuple[int, int]:
    """UE *u*'s ``(comb, cyclic shift)``: comb ``u % 4``, shift ``u // 4`` (30 UEs: 8, 8, 7, 7)."""
    return u % K_TC, u // K_TC


def srs_subcarriers(comb: int) -> np.ndarray:
    """The active-subcarrier indices of comb *comb*'s 192 REs."""
    return SRS_OFFSET + comb + K_TC * np.arange(M_SRS)


def srs_grid(n_ue: int) -> np.ndarray:
    """``(U, 1, N_SC)``: every UE's SRS symbol (spec §1)."""
    base = zc_base()
    m = np.arange(M_SRS)
    g = np.zeros((n_ue, 1, N_SC), dtype=np.complex128)
    for u in range(n_ue):
        comb, p = srs_assignment(u)
        g[u, 0, srs_subcarriers(comb)] = (
            SRS_AMP * base * np.exp(2j * np.pi * p * m / N_CS)
        )
    return g


def dmrs_port_ref(port: int) -> tuple[int, int, int]:
    """DMRS port *port*'s ``(CDM group, FD-OCC sign, TD-OCC sign)``: group ``port % 3``, the OCC pair
    from ``port // 3``."""
    o = port // 3
    return port % 3, 1 - 2 * (o % 2), 1 - 2 * (o // 2)


def dmrs_sequence(sector: int) -> np.ndarray:
    """``(2, N_SC)``: the sector's DMRS QPSK sequence on the two DMRS symbols (seeded by sector)."""
    rng = np.random.default_rng(1000 + sector)
    return (
        rng.choice([-1.0, 1.0], (2, N_SC)) + 1j * rng.choice([-1.0, 1.0], (2, N_SC))
    ) / np.sqrt(2)


def dmrs_ports(gnb: np.ndarray) -> np.ndarray:
    """``(U,)``: each UE's DMRS port, its rank among its serving sector's UEs (mod 12; -1 in outage)."""
    port = np.full(gnb.size, -1)
    for s in np.unique(gnb[gnb >= 0]):
        for k, u in enumerate(np.where(gnb == s)[0]):
            port[u] = k % 12
    return port


def dmrs_res(port: int) -> tuple[np.ndarray, np.ndarray]:
    """``(subcarriers, weight)``: port *port*'s DMRS subcarriers and its ``(2, n)`` OCC signs."""
    lam, wf, wt = dmrs_port_ref(port)
    b = np.arange(N_SC // 6)
    n = np.stack([6 * b + 2 * lam, 6 * b + 2 * lam + 1], axis=1).reshape(-1)
    kk = np.tile([1.0, float(wf)], b.size)
    w = np.stack([kk, kk * wt])
    return n, w


def pusch_grid(gnb: np.ndarray, data: np.ndarray) -> np.ndarray:
    """``(U, 14, N_SC)``: each UE's slot -- its QPSK *data* (``(U, 12, N_SC)``) on the data symbols and
    its DMRS port on symbols 2-3 (its serving sector's sequence); a UE in outage sends nothing.
    """
    ports = dmrs_ports(gnb)
    g = np.zeros((gnb.size, N_SYM, N_SC), dtype=np.complex128)
    for u in range(gnb.size):
        if gnb[u] < 0:
            continue
        g[u, list(DATA_SYMS), :] = data[u]
        n, w = dmrs_res(int(ports[u]))
        seq = dmrs_sequence(int(gnb[u]))
        for li, sym in enumerate(DMRS_SYMS):
            g[u, sym, n] = DMRS_AMP * w[li] * seq[li, n]
    return g


def qpsk_data(n_ue: int, seed: int) -> np.ndarray:
    """``(U, 12, N_SC)`` unit-energy QPSK, seeded."""
    rng = np.random.default_rng(seed)
    shape = (n_ue, len(DATA_SYMS), N_SC)
    return (
        rng.choice([-1.0, 1.0], shape) + 1j * rng.choice([-1.0, 1.0], shape)
    ) / np.sqrt(2)


def received(
    drop: dict,
    drop_id: int,
    snapshot: int,
    n: int,
    pc: lk.PowerControl,
    grid: np.ndarray,
    sector: int,
) -> np.ndarray:
    """``(N, S, N_SC)``: sector *sector*'s front-end grid of every UE's *grid*, with noise."""
    from examples.ltbf_dse.frontend import ofdm_demodulate

    ta = timing_advance(drop, snapshot, pc.gnb_idx)
    paths = sector_paths(drop, snapshot, sector, n, pc, ta)
    x = ofdm_modulate(grid)
    tail = (
        int(np.ceil((paths.tau.max() if paths.valid.any() else 0) * FS))
        + 2 * GROUP_DELAY
    )
    x = np.concatenate([x, np.zeros((x.shape[0], tail))], axis=1)
    y = render(paths, x) + noise(n * n, x.shape[1], drop_id, snapshot, sector)
    return ofdm_demodulate(y, grid.shape[1])
