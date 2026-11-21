"""Robustness of the detector (decision 16): frequency offset, multipath, timing error, near-far.

B4, N_CS 23, 64 preambles, 4 antennas, thr_q8 1531 (Pfa 1e-3, results/perf_float.json), decision
17's detector (run first with decision 10's: results/robust_v1.json).  The float
model for the sweeps (bit-exact agrees with it within Monte-Carlo error, results/perf_fixed.json),
the bit-exact model on one point of each to confirm.

    python scratch/robust.py          -> results/robust.json
"""
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prach import golden as g  # noqa: E402
from prach import nr  # noqa: E402

CFG = nr.CellConfig(n_cs=23, root0=0, n_pre=64, fmt="B4", k_start=nr.k_start_for(51, 20))
PRM = g.Params(4, CFG.n_sym, CFG.k_start, CFG.n_cs, CFG.n_pre, g.roots_for(CFG.roots), 1531)
N = 300
TA = 0.26e-6


def run(ues_fn, rng, n=N, fixed=False):
    """``ues_fn(rng) -> [Ue]``; returns per-UE hit rates, timing errors of hits, and the rate of
    occasions that report a preamble nobody sent."""
    hits, errs, ghost = None, [], 0
    for _ in range(n):
        ues = ues_fn(rng)
        rx = nr.occasion(CFG, ues, 4, rng)
        w = nr.windows(rx, CFG)
        if fixed:
            wr, wi = nr.quantize_adc(w)
            dets = [(d.preamble, d.n_peak) for d in g.search_fixed(wr, wi, PRM).dets]
        else:
            dets = [(p, k) for p, k, _ in g.search_float(w, PRM)]
        hits = np.zeros(len(ues)) if hits is None else hits
        sent = {u.preamble for u in ues}
        ghost += any(p not in sent for p, _ in dets)
        for i, u in enumerate(ues):
            cv = CFG.preamble(u.preamble)[1]
            mine = [g.delay_estimate_s(k, cv) - u.delay_s for p, k in dets if p == u.preamble]
            if mine and abs(mine[0]) < 2 * TA:
                hits[i] += 1
                errs.append(mine[0])
    errs = np.asarray(errs)
    return {"pd": [float(h / n) for h in hits], "ghost": ghost / n,
            "t_err_mean_ns": float(errs.mean() * 1e9) if len(errs) else None,
            "t_err_rms_ns": float(np.sqrt((errs ** 2).mean()) * 1e9) if len(errs) else None,
            "within_1TA": float((np.abs(errs) < TA).mean()) if len(errs) else None}


def one(snr, channel="flat", cfo=0.0):
    def f(rng):
        pre = int(rng.integers(64))
        dl = float(rng.uniform(0, CFG.max_delay_s() - 1.2e-6))   # room for the 1 us of taps
        return [nr.Ue(pre, dl, snr, cfo_hz=cfo, channel=channel)]
    return f


def near_far(gap_db, weak_snr=-16.0, guard_s=0.6e-6):
    """Two UEs on the same root, different shifts: one strong, one weak, delays uniform in the zone
    less ``guard_s`` (0.6 us: the first run; 1.0 us: the usable zone of decision 17)."""
    def f(rng):
        r = int(rng.integers(CFG.n_roots - 1))
        v1, v2 = rng.choice(CFG.per_root, 2, replace=False)
        dmax = CFG.max_delay_s() - guard_s
        return [nr.Ue(r * CFG.per_root + int(v1), float(rng.uniform(0, dmax)), weak_snr + gap_db),
                nr.Ue(r * CFG.per_root + int(v2), float(rng.uniform(0, dmax)), weak_snr)]
    return f


def main():
    rng = np.random.default_rng(2024)
    res = {"cfo_at_-20dB": {}, "tdl": {}, "near_far_weak_-16dB": {}}
    for cfo in (0, 200, 400, 600, 800, 1000, 1500):
        res["cfo_at_-20dB"][cfo] = run(one(-20.0, cfo=cfo), rng)
        print("cfo", cfo, res["cfo_at_-20dB"][cfo], flush=True)
    for snr in (-22, -20, -18, -16, -14):
        res["tdl"][snr] = run(one(snr, channel="tdl"), rng)
        print("tdl", snr, res["tdl"][snr], flush=True)
    for gap in (0, 10, 20, 30):
        res["near_far_weak_-16dB"][gap] = run(near_far(gap), rng)
        print("near-far", gap, res["near_far_weak_-16dB"][gap], flush=True)
    res["bit_exact_checks"] = {
        "cfo_600_at_-20dB": run(one(-20.0, cfo=600), rng, n=100, fixed=True),
        "tdl_at_-18dB": run(one(-18.0, channel="tdl"), rng, n=100, fixed=True),
        "near_far_20dB": run(near_far(20), rng, n=100, fixed=True),
    }
    print("bit-exact", res["bit_exact_checks"], flush=True)
    Path("results/robust.json").write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
