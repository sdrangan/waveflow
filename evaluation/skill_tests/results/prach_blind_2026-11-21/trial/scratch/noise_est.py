"""Decision 17: which noise estimate should the threshold use?  Float, B4, 4 antennas, N_CS 23.

A  the root's own profile mean (the design as built: signal included)
B  the mean over every root's profile (the strong UE's energy diluted 11x)
C  the root's profile mean excluding +-2 samples round each window's peak
D  B and C together: over every root, each window's peak neighbourhood excluded

Each threshold is recalibrated at Pfa 1e-3 per occasion (5,000 noise-only occasions), then:
near-far (two UEs on one root, the weak at -16 dB, gaps 0 / 10 / 20 / 30 dB), a strong single UE
(0 dB, flat and multipath) for ghosts -- another preamble reported -- and Pd at -22 dB.

    python scratch/noise_est.py      -> results/noise_est.json
"""
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prach import golden as g  # noqa: E402
from prach import nr  # noqa: E402

CFG = nr.CellConfig(n_cs=23, root0=0, n_pre=64, fmt="B4", k_start=nr.k_start_for(51, 20))
PRM = g.Params(4, CFG.n_sym, CFG.k_start, CFG.n_cs, CFG.n_pre, g.roots_for(CFG.roots), 1 << 30)
EXCL = 2
N_NOISE, N = 5000, 200
KINDS = "ABCD"


def profiles(w):
    x = np.fft.fft(w, axis=-1)
    bins = (CFG.k_start + np.arange(nr.L_RA)) % nr.N_FFT
    y = x[:, :, bins].sum(axis=0)
    out = []
    for u, ui in PRM.roots:
        k = np.arange(nr.L_RA)
        m = (ui * k) % nr.L_RA
        z = np.zeros((y.shape[0], g.N_CORR), dtype=complex)
        z[:, :nr.L_RA] = np.conj(y) * np.exp(1j * np.pi * u * m * (m + 1) / nr.L_RA)
        out.append((np.abs(np.fft.fft(z, axis=-1)) ** 2).sum(axis=0))
    return out


def stats(pdps):
    """Per preamble (peak, n_peak, root) and the four noise estimates per root."""
    peaks, excl_sum, excl_n = [], [], []
    for r, p in enumerate(pdps):
        mask = np.ones(g.N_CORR, bool)
        for v in range(CFG.per_root):
            pre = r * CFG.per_root + v
            if pre >= CFG.n_pre:
                break
            n0, ln = g.window(v * CFG.n_cs, CFG.n_cs)
            idx = (n0 + np.arange(ln)) % g.N_CORR
            j = int(np.argmax(p[idx]))
            peaks.append((p[idx][j], int(idx[j]), r))
            mask[(idx[j] + np.arange(-EXCL, EXCL + 1)) % g.N_CORR] = False
        excl_sum.append(p[mask].sum())
        excl_n.append(mask.sum())
    own = [p.mean() for p in pdps]
    allm = np.mean(own)
    excl = [s / n for s, n in zip(excl_sum, excl_n)]
    all_excl = sum(excl_sum) / sum(excl_n)
    noise = {"A": own, "B": [allm] * len(pdps), "C": excl, "D": [all_excl] * len(pdps)}
    return peaks, noise


def ratios(w):
    peaks, noise = stats(profiles(w))
    return {k: np.array([pk / noise[k][r] for pk, _, r in peaks]) for k in KINDS}, peaks


def main():
    rng = np.random.default_rng(77)
    mx = {k: [] for k in KINDS}
    for _ in range(N_NOISE):
        w = nr.windows(nr.occasion(CFG, [], 4, rng), CFG)
        rt, _ = ratios(w)
        for k in KINDS:
            mx[k].append(rt[k].max())
    thr = {k: float(np.quantile(mx[k], 1 - 1e-3)) for k in KINDS}
    res = {"thr": thr, "excl": EXCL, "n_noise": N_NOISE, "n": N}
    print(res, flush=True)

    def trial(ues):
        w = nr.windows(nr.occasion(CFG, ues, 4, rng), CFG)
        rt, peaks = ratios(w)
        return {k: {p for p in range(CFG.n_pre) if rt[k][p] > thr[k]} for k in KINDS}

    dmax = CFG.max_delay_s() - 1.2e-6
    for gap in (0, 10, 20, 30):
        both = {k: 0 for k in KINDS}
        ghost = {k: 0 for k in KINDS}
        for _ in range(N):
            r = int(rng.integers(CFG.n_roots - 1))
            v1, v2 = rng.choice(CFG.per_root, 2, replace=False)
            p1, p2 = r * CFG.per_root + int(v1), r * CFG.per_root + int(v2)
            det = trial([nr.Ue(p1, float(rng.uniform(0, dmax)), -16.0 + gap),
                         nr.Ue(p2, float(rng.uniform(0, dmax)), -16.0)])
            for k in KINDS:
                both[k] += p2 in det[k]
                ghost[k] += bool(det[k] - {p1, p2})
        res[f"near_far_{gap}dB"] = {"weak_found": {k: both[k] / N for k in KINDS},
                                    "ghost": {k: ghost[k] / N for k in KINDS}}
        print(gap, res[f"near_far_{gap}dB"], flush=True)
    for name, snr, ch in (("strong_flat_0dB", 0.0, "flat"), ("strong_tdl_0dB", 0.0, "tdl"),
                          ("pd_flat_-22dB", -22.0, "flat")):
        found = {k: 0 for k in KINDS}
        ghost = {k: 0 for k in KINDS}
        for _ in range(N):
            p = int(rng.integers(64))
            det = trial([nr.Ue(p, float(rng.uniform(0, dmax)), snr, channel=ch)])
            for k in KINDS:
                found[k] += p in det[k]
                ghost[k] += bool(det[k] - {p})
        res[name] = {"found": {k: found[k] / N for k in KINDS}, "ghost": {k: ghost[k] / N for k in KINDS}}
        print(name, res[name], flush=True)
    Path("results/noise_est.json").write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
