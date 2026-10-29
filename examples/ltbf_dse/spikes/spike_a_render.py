"""Spike 1.2 a (plans/ltbf_dse/ltbf_dse.md): rendering the received signals of one drop.

Three measurements, written to ``data/spikes/spike_a.json`` (local):

* **rfenv's cost.** ``ChannelApplier`` on one sector at 8x8 (64 antennas, 30 UEs, the drop's
  paths), timed and scaled to a 32x32 slot of three sectors: it interpolates every
  (antenna, UE, path) triple on its own.
* **the factored fast path.** A sector's paths share one delay per (UE, path) across its antennas
  (the synthetic array puts the per-antenna phase in ``a``), so the received samples are
  ``A @ D``: ``D`` holds each (UE, path)'s delayed waveform, interpolated once with rfenv's own
  kernel, and ``A`` (antennas x (UEs * paths)) the coefficients.  Checked against
  :func:`waveflow.rfenv.apply.reference_apply` on a 2x2 array, then timed at 32x32.
* **delays against the CP.** Over every drop and both snapshots, with each UE timing-advanced to its
  serving sector's earliest path: the fraction of UE-sector links whose delay spread exceeds the
  cyclic prefix (72 samples at 122.88 Msps, 0.586 us), and how far other sectors' paths fall
  outside the window.

    python -m examples.ltbf_dse.spikes.spike_a_render
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import numpy as np

from examples.ltbf_dse import ltbf_link as lk
from examples.ltbf_dse import scenario as sc
from waveflow.rfenv.apply import (
    DEFAULT_BETA,
    DEFAULT_FILT_LEN,
    ChannelApplier,
    fractional_delay_kernel,
    reference_apply,
    split_delay,
)
from waveflow.rfenv.paths import (
    ABSOLUTE_DELAYS,
    SCHEMA_VERSION,
    PathSet,
    channel_map,
    spec_hash,
)

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "data" / "spikes"

FS = 122.88e6
N_FFT = 1024
CP = 72
#: One slot: 14 symbols of N_FFT + CP samples.
SLOT_SAMPLES = 14 * (N_FFT + CP)
#: Rendering starts this long before every UE's timing-advanced arrival, so no delay is negative.
T0 = 2e-6


def ant_coeffs(drop: dict, snapshot: int, s: int, n: int) -> np.ndarray:
    """``(n*n, U, P)``: each path's coefficient times the synthetic array's phase (as
    :func:`ltbf_link.channel`)."""
    a = drop[f"a{snapshot}"][s] * drop[f"valid{snapshot}"][s]
    k = drop[f"k_rx{snapshot}"][s]
    r = lk.element_positions(n, float(drop["yaws"][s]))
    phase = np.einsum("dn,upd->nup", r, k) * (2.0 * np.pi / sc.WAVELENGTH)
    return a[None] * np.exp(1j * phase)


def timing_advance(drop: dict, snapshot: int, gnb: np.ndarray) -> np.ndarray:
    """``(U,)``: each UE's earliest valid path at its serving sector (in outage, at the sector with
    a path; 0 for a UE no sector hears)."""
    tau, valid = drop[f"tau{snapshot}"], drop[f"valid{snapshot}"]
    ta = np.zeros(tau.shape[1])
    for u in range(tau.shape[1]):
        heard = valid[:, u].any(axis=1)
        if not heard.any():
            continue
        s = int(gnb[u]) if gnb[u] >= 0 else int(np.argmax(heard))
        ta[u] = tau[s, u][valid[s, u]].min()
    return ta


def pathset_of(a_nup: np.ndarray, tau_up: np.ndarray, valid_up: np.ndarray) -> PathSet:
    n, u, p = a_nup.shape
    spec = {"kind": "ltbf_dse.spike_a"}
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "carrier_hz": sc.FC,
        "delays": ABSOLUTE_DELAYS,
        "channels": channel_map(
            [("bs", i) for i in range(n)], [("ue", j) for j in range(u)]
        ),
        "generator": {"name": "examples.ltbf_dse.spikes.spike_a_render"},
        "spec": spec,
        "spec_hash": spec_hash(spec),
        "created": "2026-10-28",
    }
    tau = np.broadcast_to(np.where(valid_up, tau_up, 0.0), (n, u, p))
    valid = np.broadcast_to(valid_up, (n, u, p))
    return PathSet(a_nup, tau, np.zeros((n, u, p)), valid, manifest)


def delayed(x: np.ndarray, tau_up: np.ndarray, valid_up: np.ndarray) -> np.ndarray:
    """``((U*P), L)``: every (UE, path)'s waveform delayed by ``tau`` with rfenv's interpolator,
    including its group delay ``D = filt_len / 2`` (as :class:`ChannelApplier` outputs it).
    """
    half = DEFAULT_FILT_LEN // 2
    u_n, p_n = tau_up.shape
    length = x.shape[1]
    out = np.zeros((u_n * p_n, length), dtype=np.complex128)
    for u in range(u_n):
        for p in range(p_n):
            if not valid_up[u, p]:
                continue
            k, mu = split_delay(float(tau_up[u, p]), FS, half)
            c = fractional_delay_kernel(mu, DEFAULT_FILT_LEN, DEFAULT_BETA)
            # rfenv: y(n) = sum_j c[j + half - 1] x(n - k - j), j = -half+1 .. half; with
            # i = j + half - 1 that is sum_i c[i] x(n - k + half - 1 - i) = convolve(x, c)[n - k + half - 1].
            full = np.convolve(x[u], c)
            idx = np.arange(length) - k + half - 1
            ok = (idx >= 0) & (idx < full.size)
            row = np.zeros(length, dtype=np.complex128)
            row[ok] = full[idx[ok]]
            out[u * p_n + p] = row
    return out


def render_fast(
    a_nup: np.ndarray, tau_up: np.ndarray, valid_up: np.ndarray, x: np.ndarray
):
    n, u, p = a_nup.shape
    return a_nup.reshape(n, u * p) @ delayed(x, tau_up, valid_up)


def waveform(u: int, length: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return (
        rng.choice([-1.0, 1.0], (u, length)) + 1j * rng.choice([-1.0, 1.0], (u, length))
    ) / 2


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    out: dict = {}
    drop = sc.load_drop(1)
    snapshot = 0
    pc = lk.power_control(lk.channel(drop, snapshot, 8, "100MHz"), lk.TARGETS["-6_14"])
    ta = timing_advance(drop, snapshot, pc.gnb_idx)
    s = 0
    tau_up = drop[f"tau{snapshot}"][s] - ta[:, None] + T0
    valid_up = drop[f"valid{snapshot}"][s] & (tau_up >= 0)
    tau_up = np.where(valid_up, tau_up, 0.0)

    # (1) the fast path equals rfenv's reference on a 2x2 array.
    x = waveform(tau_up.shape[0], 3000, seed=1)
    a2 = ant_coeffs(drop, snapshot, s, 2)
    ref = reference_apply(pathset_of(a2, tau_up, valid_up), x, FS)
    fast = render_fast(a2, tau_up, valid_up, x)
    rel = float(np.linalg.norm(fast - ref) / np.linalg.norm(ref))
    out["fast_vs_reference_rel_err_2x2"] = rel

    # (2) rfenv's ChannelApplier at 8x8 on a slot-length block, scaled to 32x32 x 3 sectors.
    x = waveform(tau_up.shape[0], SLOT_SAMPLES, seed=2)
    a8 = ant_coeffs(drop, snapshot, s, 8)
    ps8 = pathset_of(a8, tau_up, valid_up)
    t0 = time.perf_counter()
    app = ChannelApplier(ps8, FS)
    y8 = app.process(x, 0)
    t_rfenv8 = time.perf_counter() - t0
    out["rfenv_8x8_one_sector_slot_s"] = round(t_rfenv8, 2)
    out["rfenv_32x32_three_sectors_slot_s_est"] = round(t_rfenv8 * 16 * 3, 1)
    y8f = render_fast(a8, tau_up, valid_up, x)
    out["fast_vs_rfenv_rel_err_8x8_slot"] = float(
        np.linalg.norm(y8f - y8) / np.linalg.norm(y8)
    )

    # (3) the fast path at 32x32: one sector, one slot.
    a32 = ant_coeffs(drop, snapshot, s, 32)
    t0 = time.perf_counter()
    y32 = render_fast(a32, tau_up, valid_up, x)
    t_fast = time.perf_counter() - t0
    out["fast_32x32_one_sector_slot_s"] = round(t_fast, 2)
    out["fast_32x32_three_sectors_slot_s_est"] = round(3 * t_fast, 2)
    out["slot_samples"] = int(y32.shape[1])
    out["y32_mb_complex64"] = round(y32.size * 8 / 2**20, 1)

    # (4) delays against the CP over every drop and both snapshots.
    cp_s = CP / FS
    spreads, serving_spreads, early, late = [], [], [], []
    files = sorted(sc.DROPS_DIR.glob("drop_*.npz"))
    for f in files:
        d = sc.load_drop(int(f.stem.split("_")[1]))
        for snap in (0, 1):
            g = lk.power_control(
                lk.channel(d, snap, 8, "100MHz"), lk.TARGETS["-6_14"]
            ).gnb_idx
            ta_d = timing_advance(d, snap, g)
            tau, valid = d[f"tau{snap}"], d[f"valid{snap}"]
            for u in range(tau.shape[1]):
                if g[u] < 0:
                    continue
                for s_ in range(tau.shape[0]):
                    v = valid[s_, u]
                    if not v.any():
                        continue
                    rel_tau = tau[s_, u][v] - ta_d[u]
                    spreads.append(float(rel_tau.max() - rel_tau.min()))
                    early.append(float(rel_tau.min()))
                    late.append(float(rel_tau.max()))
                    if s_ == g[u]:
                        serving_spreads.append(float(rel_tau.max()))
    spreads, serving_spreads = np.array(spreads), np.array(serving_spreads)
    early, late = np.array(early), np.array(late)
    out["drops"] = len(files)
    out["links"] = int(spreads.size)
    out["frac_links_spread_beyond_cp"] = float(np.mean(spreads > cp_s))
    out["frac_serving_links_beyond_cp"] = float(np.mean(serving_spreads > cp_s))
    out["max_spread_us"] = float(spreads.max() * 1e6)
    out["frac_links_arriving_before_window"] = float(np.mean(early < 0))
    out["frac_links_ending_beyond_cp"] = float(np.mean(late > cp_s))
    out["min_rel_delay_us"] = float(early.min() * 1e6)
    out["max_rel_delay_us"] = float(late.max() * 1e6)
    (OUT / "spike_a.json").write_text(json.dumps(out, indent=2))
    print(json.dumps(out, indent=2))


if __name__ == "__main__":
    main()
