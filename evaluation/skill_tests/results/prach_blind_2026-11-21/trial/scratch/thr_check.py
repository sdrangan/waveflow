"""Decision 18, the check: is the Y-domain float shortcut of scratch/thr_table.py the statistic the
hardware computes?  For a few configurations, noise-only occasions through the whole bit-exact chain
(time-domain noise, the AGC'd ADC, search_fixed's profiles, the detector's statistic) against the
shortcut: the quantiles of the per-occasion maximum, and how many bit-exact occasions exceed the
table's threshold.

    python scratch/thr_check.py      -> results/thr_check.json
"""
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import thr_table as tt  # noqa: E402
from prach import golden as g  # noqa: E402
from prach import nr  # noqa: E402

N_FIXED, N_FLOAT = 300, 5000
CASES = ((1, 0, "A1"), (1, 13, "A1"), (2, 46, "A2"), (4, 23, "B4"))
QS = (0.5, 0.9, 0.99)


def fixed_max(cfg, n_ant, rng):
    prm = g.Params(n_ant, cfg.n_sym, cfg.k_start, cfg.n_cs, cfg.n_pre, g.roots_for(cfg.roots), 1 << 30)
    wr, wi = nr.quantize_adc(nr.windows(nr.occasion(cfg, [], n_ant, rng), cfg))
    tr = g.search_fixed(wr, wi, prm)
    best = 0.0
    for r, pdp in enumerate(tr.pdp):
        peaks, mask = g.window_peaks(pdp, r, cfg.n_cs, cfg.per_root, cfg.n_pre)
        mean = pdp[mask].sum() / mask.sum()
        best = max(best, max(float(p) for *_, p in peaks) / mean)
    return best


def main():
    table = json.loads(Path("results/thr_table.json").read_text())["thr"]
    rng = np.random.default_rng(1919)
    res = {}
    for n_ant, n_cs, fmt in CASES:
        cfg = nr.CellConfig(n_cs=n_cs, root0=5, n_pre=64, fmt=fmt, k_start=nr.k_start_for(51, 10))
        fx = np.array([fixed_max(cfg, n_ant, rng) for _ in range(N_FIXED)])
        refs, rw, widx, valid = tt.setup(n_cs)        # root0 = 0 there: the roots do not matter
        fl = np.concatenate([tt.max_metric(rng.normal(size=(100, n_ant, nr.L_RA))
                                           + 1j * rng.normal(size=(100, n_ant, nr.L_RA)),
                                           refs, rw, widx, valid) for _ in range(N_FLOAT // 100)])
        thr = table[str(n_ant)][str(n_cs)]
        key = f"{n_ant} ant, N_CS {n_cs}, {fmt}"
        res[key] = {"q_fixed_chain": [float(np.quantile(fx, q)) for q in QS],
                    "q_float_shortcut": [float(np.quantile(fl, q)) for q in QS],
                    "table_thr": thr, "fixed_over_thr": int((fx > thr).sum()), "n_fixed": N_FIXED}
        print(key, res[key], flush=True)
    Path("results/thr_check.json").write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
