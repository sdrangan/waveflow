"""Threshold for Pfa = 1e-3 per occasion (noise only), then Pd vs SNR -- the float reference.

    python scratch/perf.py            -> results/perf_float.json
"""
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prach import golden as g  # noqa: E402
from prach import nr  # noqa: E402

CFG = nr.CellConfig(n_cs=23, root0=0, n_pre=64, fmt="B4", k_start=nr.k_start_for(51, 20))
N_NOISE = 20000
N_PD = 500
SNRS = (-24, -22, -20, -18, -16, -14)


def params(n_ant, thr_q8):
    return g.Params(n_ant, CFG.n_sym, CFG.k_start, CFG.n_cs, CFG.n_pre, g.roots_for(CFG.roots), thr_q8)


def hit(dets, pre, dl):
    """Detected, as the right preamble, with the delay within 0.52 us (two TA steps)."""
    cv = CFG.preamble(pre)[1]
    return any(d[0] == pre and abs(g.delay_estimate_s(d[1], cv) - dl) < 0.52e-6 for d in dets)


def main():
    res = {}
    for n_ant in (4, 2):
        rng = np.random.default_rng(100 + n_ant)
        prm = params(n_ant, 1 << 30)
        mx = np.empty(N_NOISE)
        for i in range(N_NOISE):
            shape = (CFG.n_sym, n_ant, nr.N_FFT)
            w = (rng.normal(size=shape) + 1j * rng.normal(size=shape)) / np.sqrt(2)
            _, m = g.search_float(w, prm, return_metric=True)
            mx[i] = m.max()
        thr = float(np.quantile(mx, 1 - 1e-3))
        thr_q8 = int(np.ceil(thr * 256))
        prm = params(n_ant, thr_q8)
        pd, ghost = {}, {}
        for snr in SNRS:
            h = gh = 0
            for _ in range(N_PD):
                pre = int(rng.integers(64))
                dl = float(rng.uniform(0, CFG.max_delay_s() - 1.0e-6))   # the usable zone (decision 17)
                rx = nr.occasion(CFG, [nr.Ue(pre, dl, snr)], n_ant, rng)
                dets = g.search_float(nr.windows(rx, CFG), prm)
                h += hit(dets, pre, dl)
                gh += any(d[0] != pre for d in dets)
            pd[snr] = h / N_PD
            ghost[snr] = gh / N_PD
        res[n_ant] = {"thr": thr, "thr_q8": thr_q8, "n_noise": N_NOISE, "n_pd": N_PD, "pd_float": pd,
                      "other_preamble_reported": ghost}
        print(n_ant, res[n_ant], flush=True)
    Path("results").mkdir(exist_ok=True)
    Path("results/perf_float.json").write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
