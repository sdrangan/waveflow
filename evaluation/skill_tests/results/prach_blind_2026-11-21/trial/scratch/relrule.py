"""Decision 17, second step: estimator C (the root's profile less +-2 samples round each window's
peak) with a relative rule -- a window is reported only if its peak is also above ``gamma`` times the
strongest window peak on its root -- against C alone and A (as built).  Float, B4, 4 antennas, N_CS 23.

The relative rule only removes detections, so C's threshold (results/noise_est.json, Pfa 1e-3)
still bounds the false-alarm rate.

    python scratch/relrule.py      -> results/relrule.json
"""
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
import noise_est as ne  # noqa: E402
from prach import nr  # noqa: E402

CFG = ne.CFG
THR = json.loads(Path("results/noise_est.json").read_text())["thr"]
GAMMAS_DB = (-13, -16, -19, -22, -25)
N = 200


def detect(w):
    """{rule: set of preambles}.  Rules: 'A', 'C', 'C/g<dB>'."""
    peaks, noise = ne.stats(ne.profiles(w))
    vals = np.array([pk for pk, _, _ in peaks])
    roots = np.array([r for _, _, r in peaks])
    rmax = np.array([vals[roots == r].max() for r in roots])
    out = {"A": {p for p in range(len(peaks)) if vals[p] / noise["A"][roots[p]] > THR["A"]}}
    c = {p for p in range(len(peaks)) if vals[p] / noise["C"][roots[p]] > THR["C"]}
    out["C"] = c
    for gdb in GAMMAS_DB:
        g = 10 ** (gdb / 10)
        out[f"C/g{gdb}"] = {p for p in c if vals[p] > g * rmax[p]}
    return out


def main():
    rng = np.random.default_rng(1717)
    dmax = CFG.max_delay_s() - 1.2e-6
    res = {"thr": THR, "gammas_db": GAMMAS_DB, "n": N}

    def acc(cases, name):
        found, ghost = {}, {}
        for _ in range(N):
            ues, want = cases()
            det = detect(nr.windows(nr.occasion(CFG, ues, 4, rng), CFG))
            sent = {u.preamble for u in ues}
            for k, d in det.items():
                found[k] = found.get(k, 0) + (want in d)
                ghost[k] = ghost.get(k, 0) + bool(d - sent)
        res[name] = {"found": {k: v / N for k, v in found.items()},
                     "ghost": {k: v / N for k, v in ghost.items()}}
        print(name, res[name], flush=True)

    for gap in (10, 15, 20, 25):
        def nf(gap=gap):
            r = int(rng.integers(CFG.n_roots - 1))
            v1, v2 = rng.choice(CFG.per_root, 2, replace=False)
            p1, p2 = r * CFG.per_root + int(v1), r * CFG.per_root + int(v2)
            return [nr.Ue(p1, float(rng.uniform(0, dmax)), -16.0 + gap),
                    nr.Ue(p2, float(rng.uniform(0, dmax)), -16.0)], p2
        acc(nf, f"near_far_{gap}dB")
    for name, snr, ch in (("strong_flat_0dB", 0.0, "flat"), ("strong_flat_10dB", 10.0, "flat"),
                          ("strong_flat_20dB", 20.0, "flat"), ("strong_tdl_10dB", 10.0, "tdl"),
                          ("pd_flat_-22dB", -22.0, "flat")):
        def one(snr=snr, ch=ch):
            p = int(rng.integers(64))
            return [nr.Ue(p, float(rng.uniform(0, dmax)), snr, channel=ch)], p
        acc(one, name)
    Path("results/relrule.json").write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
