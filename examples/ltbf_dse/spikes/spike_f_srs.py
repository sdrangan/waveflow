"""Spike 1.2 f (plans/ltbf_dse/ltbf_dse.md, after the spec review): the SRS estimator's port leakage.

The spec's estimator per comb: de-rotate by the base sequence, IDFT to the delay domain, keep each
cyclic shift's window, DFT back.  With ``M`` comb subcarriers and 8 equally spaced shifts a port's
window is ``M/8`` taps.  Measured here on synthetic channels, every port of a comb active:

* the worst **cross-port leakage** (energy of port q's channel in port p's estimate, relative to port
  p's own) for a flat channel (a path at delay 0) and for a path half a tap late;
* the **noise gain** of each window (its estimate's noise variance relative to the per-RE noise),
  from which ``G_SRS = 10 log10(K_TC / noise gain)``.

Windows: rectangular, starting ``lead`` taps before the port's shift (a lead catches the paths the
timing advance leaves slightly early and the sinc's pre-cursor), and the same with a raised-cosine
taper of ``taper`` taps at each edge.  ``M`` = 198 (66 RBs, the spec's draft) and 192 (64 RBs).
Output ``data/spikes/spike_f.json``.

    python -m examples.ltbf_dse.spikes.spike_f_srs
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "data" / "spikes"
N_CS = 8
K_TC = 4


def zc(m: int) -> np.ndarray:
    """The largest-prime-length Zadoff-Chu sequence, cyclically extended to ``m``."""
    p = max(q for q in range(2, m + 1) if all(q % d for d in range(2, int(q**0.5) + 1)))
    k = np.arange(m) % p
    return np.exp(-1j * np.pi * 1 * k * (k + 1) / p)


def window(m: int, lead: int, taper: int) -> np.ndarray:
    """A port's delay window over ``m`` taps, starting ``lead`` taps before 0 (cyclically)."""
    w_len = m // N_CS
    w = np.zeros(m)
    shape = np.ones(w_len)
    if taper:
        ramp = 0.5 * (1 - np.cos(np.pi * (np.arange(taper) + 0.5) / taper))
        shape[:taper] = ramp
        shape[-taper:] = ramp[::-1]
    idx = (np.arange(w_len) - lead) % m
    w[idx] = shape
    return w


def estimate(y: np.ndarray, base: np.ndarray, port: int, w: np.ndarray) -> np.ndarray:
    """Port *port*'s frequency response from one comb's received ``y`` (length ``m``).

    The cyclic shift ``exp(j alpha_p n)`` moves port p's delay response to ``-p m / 8`` (mod m), a
    fractional position unless 8 divides m; the window is centred there, and port p's phase ramp is
    removed after the DFT back."""
    m = y.size
    n = np.arange(m)
    h = np.fft.ifft(y * np.conj(base))
    shift = port * m / N_CS
    # The window, moved by -shift taps: a whole-tap roll when 8 divides m, else the nearest tap.
    w_p = np.roll(w, -int(round(shift)))
    return np.fft.fft(h * w_p) * np.exp(-2j * np.pi * port * n / N_CS)


def measure(m: int, lead: int, taper: int, delay_taps: float) -> dict:
    base = zc(m)
    n = np.arange(m)
    w = window(m, lead, taper)
    resp = np.exp(-2j * np.pi * n * delay_taps / m)  # one path, flat in magnitude
    worst = -np.inf
    own = []
    for p in range(N_CS):
        for q in range(N_CS):
            alpha = 2 * np.pi * q / N_CS
            y = base * np.exp(1j * alpha * n) * resp  # port q alone
            est = estimate(y, base, p, w)
            want = resp if p == q else np.zeros(m)
            if p == q:
                own.append(np.sum(np.abs(est - want) ** 2) / np.sum(np.abs(resp) ** 2))
            else:
                worst = max(
                    worst,
                    10 * np.log10(np.sum(np.abs(est) ** 2) / np.sum(np.abs(resp) ** 2)),
                )
    # Noise gain: unit white noise through the estimator, per estimated subcarrier.
    rng = np.random.default_rng(1)
    z = (rng.standard_normal((400, m)) + 1j * rng.standard_normal((400, m))) / np.sqrt(
        2
    )
    ng = np.mean([np.mean(np.abs(estimate(zz, base, 0, w)) ** 2) for zz in z])
    return {
        "worst_cross_port_db": round(float(worst), 1),
        "own_distortion_db": round(float(10 * np.log10(max(max(own), 1e-30))), 1),
        "noise_gain": round(float(ng), 4),
        "g_srs_db": round(float(10 * np.log10(K_TC / ng)), 2),
    }


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    out = {}
    for m in (198, 192):
        for lead, taper in ((0, 0), (2, 0), (2, 3), (3, 4)):
            for d in (0.0, 0.5, 2.0):
                out[f"M{m}_lead{lead}_taper{taper}_delay{d}"] = measure(
                    m, lead, taper, d
                )
    (OUT / "spike_f.json").write_text(json.dumps(out, indent=1))
    for k, v in out.items():
        print(k, v)


if __name__ == "__main__":
    main()
