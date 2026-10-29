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
    drop = sc.load_drop(drop_id)
    n_sect = drop["yaws"].size
    n_ue = drop["a0"].shape[1]
    pc0 = lk.power_control(lk.channel(drop, 0, n, BAND), target)
    pc1 = lk.power_control(lk.channel(drop, 1, n, BAND), target)
    same = pc0.gnb_idx == pc1.gnb_idx
    served = np.array(
        [u for u in np.intersect1d(pc0.iconn, pc1.iconn) if same[u]], dtype=int
    )
    res = ChainResult(served)
    log = res.log

    # -- long term ------------------------------------------------------------------------------
    srs = sg.srs_grid(n_ue)
    blocks = []
    for s in range(n_sect):
        y = sg.received(drop, drop_id, 0, n, pc0, srs, s)[:, 0, :]
        blocks.append(ce.srs_blocks(y, pc0.iconn, log))
    ltin = ce.long_term_from_srs(blocks, pc0, log)
    pos = {int(u): k for k, u in enumerate(pc0.iconn)}
    configs: dict = {}
    for s in range(n_sect):
        for u, a in lt.sector_a(ltin, s, lt.Solve(), None).items():
            configs.setdefault(("exact",), {})[u] = a
        for sv in solves:
            for u, a_t in lt.sector_a(ltin, s, sv, t_max).items():
                for t in t_eval or range(1, t_max + 1):
                    configs.setdefault((sv.form, sv.regime, t), {})[u] = a_t[t - 1]
    proj: dict = {}
    for key, per_ue in configs.items():
        for r in ranks:
            g_all, gp_all = {}, {}
            for u, a in per_ue.items():
                g = lt.projection(a, ltin.srs_own[u], r)
                s = int(pc0.gnb_idx[u])
                cols = slice(pos[u] * lk.N_SRS, (pos[u] + 1) * lk.N_SRS)
                gp_all[u], _ = whitened(g, ltin.h_alpha[s], cols)
                g_all[u] = g
            proj[key + (r,)] = gp_all
            hf = lk.scaled(lk.channel(drop, 1, n, BAND), pc1)
            res.genie[key + (r,)] = lt.sinr_lt_db(g_all, hf, pc1, served)

    # -- real time --------------------------------------------------------------------------------
    data = sg.qpsk_data(n_ue, seed=1000 * drop_id + data_seed)
    grid = sg.pusch_grid(pc1.gnb_idx, data)
    ports = sg.dmrs_ports(pc1.gnb_idx)
    for s in range(n_sect):
        mine = [u for u in served if pc1.gnb_idx[u] == s]
        if not mine:
            continue
        y = sg.received(drop, drop_id, 1, n, pc1, grid, s)  # (N, 14, 792)
        yf = y.reshape(y.shape[0], -1)
        for key, gp_all in proj.items():
            for u in mine:
                z = (gp_all[u] @ yf).reshape(-1, sg.N_SYM, sg.N_SC)
                _h, w = ce.dmrs_weights(z[:, list(sg.DMRS_SYMS)], int(ports[u]), s, log)
                xh = equalize(z[:, list(sg.DATA_SYMS)], w)
                res.metrics.setdefault(key, {})[u] = measure(xh, data[u])
    return res
