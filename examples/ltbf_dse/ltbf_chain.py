"""ltbf_chain.py -- the LTBF receiver end to end, in floating point, on rendered signals (spec §3).

``plans/ltbf_dse/ltbf_dse.md`` step 2.3.  One drop, one array, one SINR target:

1. **long term (snapshot 0):** every UE's SRS rendered to each sector's array, the SRS estimated
   (:func:`chest.srs_blocks`), ``Ĥα`` per sector; ``Aᵢ`` by a direct solve (the reference) or by CG
   (``ltbf.cg_iterates``, every ``t``); ``Gᵢ = (AᵢΦᵢ)ᴴ``; ``R̃ᵢ = GᵢGᵢᴴ + Σ_{j≠i}(GᵢĤα,ⱼ)(·)ᴴ``; its
   Cholesky factor ``Lᵢ`` and the whitened projection ``G'ᵢ = Lᵢ⁻¹Gᵢ``;
2. **real time (snapshot 1):** every UE's slot rendered to each sector; per served UE
   ``z' = G'ᵢ y`` on every RE; the DMRS estimate and the weights (:func:`chest.dmrs_weights`);
   ``x̂ = Σ_r conj(w_r) z'_r`` on the data REs;
3. **measurement:** (a) the MMSE SINR ``1/mean|x̂ - x|² - 1``, (b) the scale-invariant
   ``|ρ|²/(1 - |ρ|²)``, EVM, uncoded QPSK BER, and (c) the paper's genie SINR for the same ``Gᵢ``
   (``ltbf.sinr_lt_db``: the true per-frequency ``Q``).

A UE is **served** when it is connected at both snapshots with the same serving sector (its
projection belongs to that sector's array).  Every estimator's operations go into one
:class:`~examples.ltbf_dse.opcount.OpLog`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from examples.ltbf_dse import chest as ce
from examples.ltbf_dse import ltbf as lt
from examples.ltbf_dse import ltbf_link as lk
from examples.ltbf_dse import scenario as sc
from examples.ltbf_dse import signals as sg
from examples.ltbf_dse.opcount import OpLog

BAND = "100MHz"


def whitened(
    g: np.ndarray, h_alpha: np.ndarray, cols_i: slice
) -> tuple[np.ndarray, np.ndarray]:
    """``(G', R̃)``: ``R̃ = GGᴴ + Σ_{j≠i}(GĤα,ⱼ)(GĤα,ⱼ)ᴴ`` (UE i's columns *cols_i* of ``Ĥα`` left
    out) and ``G' = L⁻¹G`` with ``R̃ = LLᴴ`` (Cholesky), so ``G'R̃'G'ᴴ = I``."""
    gh = g @ h_alpha
    keep = np.ones(h_alpha.shape[1], dtype=bool)
    keep[cols_i] = False
    rt = g @ g.conj().T + gh[:, keep] @ gh[:, keep].conj().T
    chol = np.linalg.cholesky(rt)
    return np.linalg.solve(chol, g), rt


def equalize(z: np.ndarray, w: np.ndarray) -> np.ndarray:
    """``x̂ = Σ_r conj(w_r) z_r``: ``z`` ``(r, S, 792)``, ``w`` ``(r, 792)``."""
    return np.einsum("rf,rsf->sf", w.conj(), z)


def measure(xh: np.ndarray, x: np.ndarray) -> dict[str, float]:
    """The SINRs (dB), EVM and uncoded QPSK BER of an estimate ``xh`` of unit-energy QPSK ``x``."""
    err = np.mean(np.abs(xh - x) ** 2)
    rho = np.vdot(x, xh) / np.sqrt(np.vdot(x, x).real * np.vdot(xh, xh).real)
    r2 = min(float(np.abs(rho) ** 2), 1 - 1e-15)
    sinr_a = 1.0 / max(err, 1e-15) - 1.0
    bits_tx = np.stack([x.real > 0, x.imag > 0])
    bits_rx = np.stack([xh.real > 0, xh.imag > 0])
    return {
        "sinr_mmse_db": float(10 * np.log10(max(sinr_a, 1e-6))),
        "sinr_scale_inv_db": float(10 * np.log10(r2 / (1 - r2))),
        "evm_db": float(10 * np.log10(max(err, 1e-15))),
        "ber": float(np.mean(bits_tx != bits_rx)),
    }


@dataclass
class ChainResult:
    """Per configuration key -> per served UE -> metrics; keys ``("exact", r)`` or
    ``(form, regime, t, r)``."""

    served: np.ndarray
    metrics: dict = field(default_factory=dict)
    genie: dict = field(default_factory=dict)
    log: OpLog = field(default_factory=OpLog)


@dataclass
class Prepared:
    """What every configuration of one (drop, array, target) shares: the SRS estimates and the
    long-term inputs (snapshot 0), the rendered slot of every sector (snapshot 1), the data.
    """

    drop: dict
    drop_id: int
    n: int
    pc0: lk.PowerControl
    pc1: lk.PowerControl
    served: np.ndarray
    ltin: lk.LongTerm
    pos: dict
    data: np.ndarray
    ports: np.ndarray
    y: dict  # sector -> (N, 14, 792)
    hf: np.ndarray  # the scaled snapshot-1 channel, for the genie SINR
    log: OpLog


def prepare(
    drop_id: int, n: int, target: tuple[float, float], data_seed: int = 0
) -> Prepared:
    """Render and estimate once: the SRS (snapshot 0) into ``Ĥα``, and every sector's slot."""
    drop = sc.load_drop(drop_id)
    n_sect = drop["yaws"].size
    n_ue = drop["a0"].shape[1]
    pc0 = lk.power_control(lk.channel(drop, 0, n, BAND), target)
    pc1 = lk.power_control(lk.channel(drop, 1, n, BAND), target)
    same = pc0.gnb_idx == pc1.gnb_idx
    served = np.array(
        [u for u in np.intersect1d(pc0.iconn, pc1.iconn) if same[u]], dtype=int
    )
    log = OpLog()
    srs = sg.srs_grid(n_ue)
    blocks = [
        ce.srs_blocks(
            sg.received(drop, drop_id, 0, n, pc0, srs, s)[:, 0, :], pc0.iconn, log
        )
        for s in range(n_sect)
    ]
    ltin = ce.long_term_from_srs(blocks, pc0, log)
    data = sg.qpsk_data(n_ue, seed=1000 * drop_id + data_seed)
    grid = sg.pusch_grid(pc1.gnb_idx, data)
    y = {
        s: sg.received(drop, drop_id, 1, n, pc1, grid, s)
        for s in range(n_sect)
        if any(pc1.gnb_idx[u] == s for u in served)
    }
    hf = lk.scaled(lk.channel(drop, 1, n, BAND), pc1)
    pos = {int(u): k for k, u in enumerate(pc0.iconn)}
    return Prepared(
        drop,
        drop_id,
        n,
        pc0,
        pc1,
        served,
        ltin,
        pos,
        data,
        sg.dmrs_ports(pc1.gnb_idx),
        y,
        hf,
        log,
    )


def project(
    prep: Prepared, a_per_ue: dict[int, np.ndarray], r: int
) -> tuple[dict, dict]:
    """``(G, G')`` per UE from its ``Aᵢ``: ``Gᵢ = (AᵢΦᵢ)ᴴ`` and the whitened ``G'ᵢ = Lᵢ⁻¹Gᵢ``."""
    g_all, gp_all = {}, {}
    for u, a in a_per_ue.items():
        g = lt.projection(a, prep.ltin.srs_own[u], r)
        s = int(prep.pc0.gnb_idx[u])
        cols = slice(prep.pos[u] * lk.N_SRS, (prep.pos[u] + 1) * lk.N_SRS)
        gp_all[u], _ = whitened(g, prep.ltin.h_alpha[s], cols)
        g_all[u] = g
    return g_all, gp_all


def real_time(
    prep: Prepared, gp_all: dict[int, np.ndarray], project_fn=None, equalize_fn=None
):
    """Per served UE with a projection: ``z' = G'y``, the DMRS weights, ``x̂`` and its metrics.
    *project_fn(u, s)* and *equalize_fn(z, w)* replace the float projection and equalizer (the
    fixed-point chain's engines)."""
    out = {}
    for s, y in prep.y.items():
        yf = y.reshape(y.shape[0], -1)
        for u in prep.served:
            u = int(u)
            if prep.pc1.gnb_idx[u] != s or u not in gp_all:
                continue
            if project_fn is None:
                z = (gp_all[u] @ yf).reshape(-1, sg.N_SYM, sg.N_SC)
            else:
                z = project_fn(u, s)
            _h, w = ce.dmrs_weights(
                z[:, list(sg.DMRS_SYMS)], int(prep.ports[u]), s, prep.log
            )
            zd = z[:, list(sg.DATA_SYMS)]
            xh = equalize(zd, w) if equalize_fn is None else equalize_fn(zd, w)
            out[u] = measure(xh, prep.data[u])
    return out


def run_chain(
    drop_id: int,
    n: int,
    target: tuple[float, float],
    *,
    ranks: tuple[int, ...] = (1, 2),
    t_max: int = 10,
    solves: tuple[lt.Solve, ...] = (lt.Solve("factored", 1),),
    t_eval: tuple[int, ...] | None = None,
    data_seed: int = 0,
) -> ChainResult:
    """The whole chain on drop *drop_id*'s ``n x n`` arrays at *target*; CG configurations for
    every *solve* and every ``t`` in *t_eval* (default all ``1..t_max``), plus the exact reference.
    """
    prep = prepare(drop_id, n, target, data_seed)
    res = ChainResult(prep.served, log=prep.log)
    configs: dict = {}
    for s in range(len(prep.ltin.own)):
        for u, a in lt.sector_a(prep.ltin, s, lt.Solve(), None).items():
            configs.setdefault(("exact",), {})[u] = a
        for sv in solves:
            for u, a_t in lt.sector_a(prep.ltin, s, sv, t_max).items():
                for t in t_eval or range(1, t_max + 1):
                    configs.setdefault((sv.form, sv.regime, t), {})[u] = a_t[t - 1]
    for key, per_ue in configs.items():
        for r in ranks:
            g_all, gp_all = project(prep, per_ue, r)
            res.genie[key + (r,)] = lt.sinr_lt_db(g_all, prep.hf, prep.pc1, prep.served)
            res.metrics[key + (r,)] = real_time(prep, gp_all)
    return res
