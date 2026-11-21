"""Spot checks of the bit-exact chain against the float one (4 antennas, B4, N_CS 23, thr_q8 1531):
noise-only false alarms, Pd at -24 / -22 dB, and the peak/mean ratio error on every detection both
report.

    python scratch/perf_fixed.py      -> results/perf_fixed.json
"""
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prach import golden as g  # noqa: E402
from prach import nr  # noqa: E402

CFG = nr.CellConfig(n_cs=23, root0=0, n_pre=64, fmt="B4", k_start=nr.k_start_for(51, 20))
THR_Q8 = 1531
N_NOISE, N_SIG = 300, 150


def main():
    prm = g.Params(4, CFG.n_sym, CFG.k_start, CFG.n_cs, CFG.n_pre, g.roots_for(CFG.roots), THR_Q8)
    rng = np.random.default_rng(5)
    res = {"thr_q8": THR_Q8}
    fa_fx = fa_fl = 0
    for _ in range(N_NOISE):
        rx = nr.occasion(CFG, [], 4, rng)
        w = nr.windows(rx, CFG)
        wr, wi = nr.quantize_adc(w)
        fa_fx += bool(g.search_fixed(wr, wi, prm).dets)
        fa_fl += bool(g.search_float(wr + 1j * wi, prm))
    res["noise_only"] = {"n": N_NOISE, "false_alarm_fixed": fa_fx, "false_alarm_float": fa_fl}
    print(res, flush=True)
    errs = []
    for snr in (-24, -22):
        h_fx = h_fl = agree = 0
        for _ in range(N_SIG):
            pre = int(rng.integers(64))
            dl = float(rng.uniform(0, CFG.max_delay_s() - 1.0e-6))   # the usable zone (decision 17)
            rx = nr.occasion(CFG, [nr.Ue(pre, dl, snr)], 4, rng, pusch_db=10.0)
            w = nr.windows(rx, CFG)
            wr, wi = nr.quantize_adc(w)
            fx = g.search_fixed(wr, wi, prm).dets
            fl = g.search_float(wr + 1j * wi, prm)
            h_fx += any(d.preamble == pre for d in fx)
            h_fl += any(d[0] == pre for d in fl)
            agree += {d.preamble for d in fx} == {d[0] for d in fl}
            flm = {d[0]: d[2] for d in fl}
            for d in fx:
                if d.preamble in flm:
                    errs.append(abs(d.peak / (d.noise / d.n_noise) - flm[d.preamble]) / flm[d.preamble])
        res[f"snr_{snr}"] = {"n": N_SIG, "pd_fixed": h_fx / N_SIG, "pd_float_same_input": h_fl / N_SIG,
                             "same_detections": agree / N_SIG}
        print(res, flush=True)
    res["ratio_rel_err"] = {"max": float(np.max(errs)), "mean": float(np.mean(errs)), "n": len(errs)}
    print(res, flush=True)
    Path("results/perf_fixed.json").write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
