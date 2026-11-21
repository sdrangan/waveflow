"""Decision 17, last step.  results/taper.json showed that a strong UE at zero delay lights the
previous shift's window even with the design as built (A: 31 % of occasions at 20 dB): the first
Dirichlet sidelobe (-13 dB, 1.43 ZC samples = 2.6 profile samples early) falls past WIN_MARGIN = 2.
Here: WIN_MARGIN 2 or 4, crossed with the estimator (A as built; C, the root's profile less +-2
samples round each window peak) and a relative rule (a window's peak above gamma x the strongest
window peak on its root).  Float, B4, 4 antennas, N_CS 23; each (margin, variant) recalibrated at
Pfa 1e-3 per occasion.

    python scratch/margin_rel.py      -> results/margin_rel.json
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
MARGINS = (2, 4)
#: variant -> (estimator, gamma dB or None)
VARIANTS = {"A": ("own", None), "C": ("excl", None), "C/g-16": ("excl", -16),
            "C/g-19": ("excl", -19), "C/g-22": ("excl", -22)}
N_NOISE, N = 3000, 200
k = np.arange(nr.L_RA)


def profiles(w):
    x = np.fft.fft(w, axis=-1)
    y = x[:, :, (CFG.k_start + k) % nr.N_FFT].sum(axis=0)
    out = []
    for u, ui in ROOTS:
        m = (ui * k) % nr.L_RA
        z = np.zeros((y.shape[0], g.N_CORR), dtype=complex)
        z[:, :nr.L_RA] = np.conj(y) * np.exp(1j * np.pi * u * m * (m + 1) / nr.L_RA)
        out.append((np.abs(np.fft.fft(z, axis=-1)) ** 2).sum(axis=0))
    return out


def metrics(pdps, margin):
    g.WIN_MARGIN = margin                       # golden.window reads the module constant
    vals, roots, own, excl = [], [], [], []
    for r, p in enumerate(pdps):
        mask = np.ones(g.N_CORR, bool)
        for v in range(CFG.per_root):
            if r * CFG.per_root + v >= CFG.n_pre:
                break
            n0, ln = g.window(v * CFG.n_cs, CFG.n_cs)
            idx = (n0 + np.arange(ln)) % g.N_CORR
            j = int(np.argmax(p[idx]))
            vals.append(p[idx][j])
            roots.append(r)
            mask[(idx[j] + np.arange(-2, 3)) % g.N_CORR] = False
        own.append(p.mean())
        excl.append(p[mask].mean())
    vals, roots = np.array(vals), np.array(roots)
    rmax = np.array([vals[roots == r].max() for r in roots])
    res = {}
    for var, (est, gdb) in VARIANTS.items():
        nz = np.array([(own if est == "own" else excl)[r] for r in roots])
        ok = np.ones(len(vals), bool) if gdb is None else vals > 10 ** (gdb / 10) * rmax
        res[var] = (vals / nz, ok)
    return res


def main():
    rng = np.random.default_rng(9090)
    keys = [(m, v) for m in MARGINS for v in VARIANTS]
    mx = {kk: [] for kk in keys}
    for _ in range(N_NOISE):
        pd = profiles(nr.windows(nr.occasion(CFG, [], 4, rng), CFG))
        for m in MARGINS:
            for v, (rt, _) in metrics(pd, m).items():
                mx[(m, v)].append(rt.max())
    thr = {kk: float(np.quantile(mx[kk], 1 - 1e-3)) for kk in keys}
    res = {"thr": {f"m{m} {v}": t for (m, v), t in thr.items()}, "n_noise": N_NOISE, "n": N}
    print("thr", res["thr"], flush=True)
    zone = CFG.max_delay_s()
    dmax = zone - 1.2e-6

    def acc(cases, name):
        found = {kk: 0 for kk in keys}
        ghost = {kk: 0 for kk in keys}
        for _ in range(N):
            ues, want = cases()
            pd = profiles(nr.windows(nr.occasion(CFG, ues, 4, rng), CFG))
            sent = {u.preamble for u in ues}
            for m in MARGINS:
                for v, (rt, ok) in metrics(pd, m).items():
                    d = {int(p) for p in np.nonzero((rt > thr[(m, v)]) & ok)[0]}
                    found[(m, v)] += want in d
                    ghost[(m, v)] += bool(d - sent)
        res[name] = {"found": {f"m{m} {v}": found[(m, v)] / N for m, v in keys},
                     "ghost": {f"m{m} {v}": ghost[(m, v)] / N for m, v in keys}}
        print(name, res[name], flush=True)

    for gap in (10, 15, 20):
        def nf(gap=gap):
            r = int(rng.integers(CFG.n_roots - 1))
            v1, v2 = rng.choice(CFG.per_root, 2, replace=False)
            p1, p2 = r * CFG.per_root + int(v1), r * CFG.per_root + int(v2)
            return [nr.Ue(p1, float(rng.uniform(0, dmax)), -16.0 + gap),
                    nr.Ue(p2, float(rng.uniform(0, dmax)), -16.0)], p2
        acc(nf, f"near_far_{gap}dB")
    # delay None: uniform in [0, dmax]; a number: that delay
    for name, snr, ch, dl in (("strong_flat_20dB", 20.0, "flat", None),
                              ("strong_flat_20dB_zero_delay", 20.0, "flat", 0.0),
                              ("strong_flat_20dB_delay_zone-0.8us", 20.0, "flat", zone - 0.8e-6),
                              ("strong_flat_0dB_zero_delay", 0.0, "flat", 0.0),
                              ("strong_tdl_10dB", 10.0, "tdl", None),
                              ("pd_flat_-22dB", -22.0, "flat", None),
                              ("pd_tdl_-18dB", -18.0, "tdl", None)):
        def one(snr=snr, ch=ch, dl=dl):
            p = int(rng.integers(64))
            d = float(rng.uniform(0, dmax)) if dl is None else dl
            return [nr.Ue(p, d, snr, channel=ch)], p
        acc(one, name)
    Path("results/margin_rel.json").write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
