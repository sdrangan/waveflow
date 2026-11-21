"""Decision 17, third step: the ghosts that estimator C exposes are the strong UE's Dirichlet sidelobes
(a 139-point sequence interpolated to 256 points: -13 dB at 1.4 ZC samples, decaying as 1/(pi d)), so
a relative rule must sit near -13 dB to kill them.  Here: a spectral taper on Y (one real ROM of 139
weights, applied once per antenna in extract) to lower the sidelobes, with estimator C.

Variants (float, B4, 4 antennas, N_CS 23), each recalibrated at Pfa 1e-3 per occasion:
  A        as built: no taper, own-root mean
  C        no taper, own root less +-2 samples round each window peak
  C/g-16   C, and the peak above -16 dB of the root's strongest window peak
  hann     Hann taper, own root less +-EXCL samples, EXCL 4
  hamm     Hamming taper, same
  hamm/g-25, hann/g-25   with a relative rule at -25 dB

    python scratch/taper.py      -> results/taper.json
"""
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prach import golden as g  # noqa: E402
from prach import nr  # noqa: E402

CFG = nr.CellConfig(n_cs=23, root0=0, n_pre=64, fmt="B4", k_start=nr.k_start_for(51, 20))
ROOTS = g.roots_for(CFG.roots)
N_NOISE, N = 4000, 200
k = np.arange(nr.L_RA)
TAPERS = {"none": np.ones(nr.L_RA),
          "hann": 0.5 - 0.5 * np.cos(2 * np.pi * (k + 1) / (nr.L_RA + 1)),
          "hamm": 0.54 - 0.46 * np.cos(2 * np.pi * k / (nr.L_RA - 1))}
#: variant -> (taper, exclusion half-width, relative rule dB or None, noise "own"/"excl")
VARIANTS = {"A": ("none", 0, None, "own"), "C": ("none", 2, None, "excl"),
            "C/g-16": ("none", 2, -16, "excl"),
            "hann": ("hann", 4, None, "excl"), "hamm": ("hamm", 4, None, "excl"),
            "hann/g-25": ("hann", 4, -25, "excl"), "hamm/g-25": ("hamm", 4, -25, "excl")}


def per_taper(w):
    """taper -> per root (pdp)"""
    x = np.fft.fft(w, axis=-1)
    bins = (CFG.k_start + k) % nr.N_FFT
    y0 = x[:, :, bins].sum(axis=0)
    out = {}
    for name, t in TAPERS.items():
        y = y0 * t
        pd = []
        for u, ui in ROOTS:
            m = (ui * k) % nr.L_RA
            z = np.zeros((y.shape[0], g.N_CORR), dtype=complex)
            z[:, :nr.L_RA] = np.conj(y) * np.exp(1j * np.pi * u * m * (m + 1) / nr.L_RA)
            pd.append((np.abs(np.fft.fft(z, axis=-1)) ** 2).sum(axis=0))
        out[name] = pd
    return out


def metrics(w):
    """variant -> (ratio per preamble, relative-rule pass per preamble)"""
    pdps = per_taper(w)
    res = {}
    cache = {}
    for var, (tap, excl, gdb, est) in VARIANTS.items():
        key = (tap, excl)
        if key not in cache:
            vals, roots, noise = [], [], []
            for r, p in enumerate(pdps[tap]):
                mask = np.ones(g.N_CORR, bool)
                for v in range(CFG.per_root):
                    if r * CFG.per_root + v >= CFG.n_pre:
                        break
                    n0, ln = g.window(v * CFG.n_cs, CFG.n_cs)
                    idx = (n0 + np.arange(ln)) % g.N_CORR
                    j = int(np.argmax(p[idx]))
                    vals.append(p[idx][j])
                    roots.append(r)
                    mask[(idx[j] + np.arange(-excl, excl + 1)) % g.N_CORR] = False
                noise.append((p.mean(), p[mask].mean()))
            vals, roots = np.array(vals), np.array(roots)
            rmax = np.array([vals[roots == r].max() for r in roots])
            cache[key] = (vals, roots, noise, rmax)
        vals, roots, noise, rmax = cache[key]
        nz = np.array([noise[r][0 if est == "own" else 1] for r in roots])
        ok = np.ones(len(vals), bool) if gdb is None else vals > 10 ** (gdb / 10) * rmax
        res[var] = (vals / nz, ok)
    return res


def main():
    rng = np.random.default_rng(4242)
    mx = {v: [] for v in VARIANTS}
    for _ in range(N_NOISE):
        m = metrics(nr.windows(nr.occasion(CFG, [], 4, rng), CFG))
        for v, (rt, _) in m.items():
            mx[v].append(rt.max())
    thr = {v: float(np.quantile(mx[v], 1 - 1e-3)) for v in VARIANTS}
    res = {"thr": thr, "n_noise": N_NOISE, "n": N}
    print("thr", thr, flush=True)
    dmax = CFG.max_delay_s() - 1.2e-6

    def acc(cases, name):
        found = {v: 0 for v in VARIANTS}
        ghost = {v: 0 for v in VARIANTS}
        for _ in range(N):
            ues, want = cases()
            m = metrics(nr.windows(nr.occasion(CFG, ues, 4, rng), CFG))
            sent = {u.preamble for u in ues}
            for v, (rt, ok) in m.items():
                d = {int(p) for p in np.nonzero((rt > thr[v]) & ok)[0]}
                found[v] += want in d
                ghost[v] += bool(d - sent)
        res[name] = {"found": {v: found[v] / N for v in VARIANTS},
                     "ghost": {v: ghost[v] / N for v in VARIANTS}}
        print(name, res[name], flush=True)

    for gap in (10, 20, 25, 30):
        def nf(gap=gap):
            r = int(rng.integers(CFG.n_roots - 1))
            v1, v2 = rng.choice(CFG.per_root, 2, replace=False)
            p1, p2 = r * CFG.per_root + int(v1), r * CFG.per_root + int(v2)
            return [nr.Ue(p1, float(rng.uniform(0, dmax)), -16.0 + gap),
                    nr.Ue(p2, float(rng.uniform(0, dmax)), -16.0)], p2
        acc(nf, f"near_far_{gap}dB")
    for name, snr, ch, dl in (("strong_flat_0dB", 0.0, "flat", None),
                              ("strong_flat_20dB", 20.0, "flat", None),
                              ("strong_flat_20dB_zero_delay", 20.0, "flat", 0.0),
                              ("strong_tdl_10dB", 10.0, "tdl", None),
                              ("pd_flat_-22dB", -22.0, "flat", None),
                              ("pd_flat_-24dB", -24.0, "flat", None),
                              ("pd_tdl_-18dB", -18.0, "tdl", None)):
        def one(snr=snr, ch=ch, dl=dl):
            p = int(rng.integers(64))
            d = float(rng.uniform(0, dmax)) if dl is None else dl
            return [nr.Ue(p, d, snr, channel=ch)], p
        acc(one, name)
    Path("results/taper.json").write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
