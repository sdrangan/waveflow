"""Decision 18: the threshold table the host sends, thr_q8(n_ant, N_CS), 64 preambles, Pfa 1e-3 per
occasion, decision 17's detector (golden.search_float's statistic, vectorised over occasions).

The noise is simulated as Y directly: white complex Gaussian on the 139 subcarriers per antenna.
Noise through the FFT-1024 and the coherent repetition sum is again white Gaussian, and the statistic
is a ratio, so neither the format (n_sym) nor the scale matters.  Checked against the full chain at
N_CS 23 (results/perf_float.json: 1531 for 4 antennas, 2465 for 2).

    python scratch/thr_table.py      -> results/thr_table.json
"""
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from prach import golden as g  # noqa: E402
from prach import nr  # noqa: E402

N_OCC = 20000
BATCH = 100
N_PRE = 64
PFA = 1e-3


def setup(n_cs: int):
    cfg = nr.CellConfig(n_cs=n_cs, root0=0, n_pre=N_PRE, fmt="B4")
    k = np.arange(nr.L_RA)
    refs = []
    for u, ui in g.roots_for(cfg.roots):
        m = (ui * k) % nr.L_RA
        refs.append(np.exp(1j * np.pi * u * m * (m + 1) / nr.L_RA))
    rw, idx = [], []
    for p in range(N_PRE):
        r, v = divmod(p, cfg.per_root)
        n0, ln = g.window(v * n_cs, n_cs)
        rw.append(r)
        idx.append((n0 + np.arange(ln)) % g.N_CORR)
    lmax = max(len(i) for i in idx)
    widx = np.zeros((N_PRE, lmax), dtype=np.int64)
    valid = np.zeros((N_PRE, lmax), dtype=bool)
    for p, i in enumerate(idx):
        widx[p, :len(i)] = i
        valid[p, :len(i)] = True
    return np.array(refs), np.array(rw), widx, valid


def max_metric(y, refs, rw, widx, valid):
    """y: (B, A, 139) -> the largest window statistic per occasion, (B,)."""
    b = y.shape[0]
    z = np.zeros((b, len(refs), y.shape[1], g.N_CORR), dtype=complex)
    z[..., :nr.L_RA] = np.conj(y)[:, None] * refs[None, :, None, :]
    pdp = (np.abs(np.fft.fft(z, axis=-1)) ** 2).sum(axis=2)              # (B, R, 256)
    vals = pdp[:, rw[:, None], widx]                                     # (B, P, L)
    vals = np.where(valid[None], vals, -1.0)
    j = vals.argmax(axis=-1)
    peak = np.take_along_axis(vals, j[..., None], -1)[..., 0]            # (B, P), first maximum
    npk = widx[np.arange(len(rw))[None, :], j]                           # (B, P)
    mask = np.ones_like(pdp, dtype=bool)
    bb = np.arange(b)[:, None]
    for d in range(-g.NOISE_EXCL, g.NOISE_EXCL + 1):
        mask[bb, rw[None, :], (npk + d) % g.N_CORR] = False
    mean = (pdp * mask).sum(-1) / mask.sum(-1)                           # (B, R)
    return (peak / mean[:, rw]).max(axis=1)


def main():
    rng = np.random.default_rng(1818)
    res = {"n_occ": N_OCC, "pfa": PFA, "n_pre": N_PRE, "thr_q8": {}, "thr": {}}
    for n_ant in (4, 2, 1):
        res["thr_q8"][n_ant], res["thr"][n_ant] = {}, {}
        for n_cs in nr.NCS_TABLE_139:
            refs, rw, widx, valid = setup(n_cs)
            mx = []
            for _ in range(N_OCC // BATCH):
                shape = (BATCH, n_ant, nr.L_RA)
                y = rng.normal(size=shape) + 1j * rng.normal(size=shape)
                mx.append(max_metric(y, refs, rw, widx, valid))
            thr = float(np.quantile(np.concatenate(mx), 1 - PFA))
            res["thr"][n_ant][n_cs] = thr
            res["thr_q8"][n_ant][n_cs] = int(np.ceil(thr * 256))
            print(n_ant, n_cs, res["thr_q8"][n_ant][n_cs], flush=True)
    Path("results/thr_table.json").write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
