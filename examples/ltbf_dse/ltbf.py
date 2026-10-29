"""ltbf.py -- floating-point long-term beamforming: the reference every approximation is scored against.

``plans/mimo_cg/ltbf_cg_dse.md`` step 2.1.  The LTBF paper's long-term update (``salsa_cap.py``
``compute_lt_bf``, canonical form; §III-B.1) and its SINR (``compute_sinr_lt``), per sector:

* **exact:** ``A = Q^-1 B`` by a direct solve;
* **CG:** multi-right-hand-side CG from ``X0 = 0``, each column with its own ``omega``, ``nu``, with
  ``S = Q D`` (explicit) or ``S = D + H_alpha (H_alpha^H D)`` (factored).  Every iterate is kept, so
  one run gives every ``t`` up to ``t_max``;
* **regime 1:** ``B`` = the SRS blocks of the sector's own UEs; **regime 2:** ``B = I``, the
  iterate symmetrized, then ``A_i = X H_i``;
* **projection:** ``M_i = H_i^H A_i`` symmetrized, its top-``r`` eigenvectors ``Phi``,
  ``G_i = (A_i Phi)^H``;
* **SINR** one ``T_LT`` later: per sector and frequency, ``Q = I + Hc Hc^H`` over the connected UEs,
  ``s = (G h)^H (G Q G^H)^-1 (G h)``, ``SINR = s / (1 - s)``; per UE ``10 log10`` of the frequency
  mean.  ``G Q G^H = G G^H + (G Hc)(G Hc)^H`` is evaluated in ``r x r``, never ``N x N``.

Instantaneous MMSE (the paper's context curve) uses the Gram form: ``s_i`` is the ``i``-th diagonal
entry of ``Hc^H Hc (I + Hc^H Hc)^-1``.

**Provenance.** Copied from ``paper/cg_dse`` (``examples/ltbf_cg/``, ``ba757884``) by
``plans/ltbf_dse/ltbf_dse.md`` step 0.5; the ``plans/mimo_cg/ltbf_cg_dse.md`` it cites lives on that
branch (``git show paper/cg_dse:plans/mimo_cg/ltbf_cg_dse.md``).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from examples.ltbf_dse import ltbf_link as lk

ApplyS = Callable[[np.ndarray], np.ndarray]

#: The LTBF SINR floor (-60 dB): a fixed-point solve can return ``A_i = 0`` (every entry below
#: one LSB), whose projection receives nothing.
SINR_FLOOR = 1e-6


def explicit_s(h_alpha: np.ndarray) -> ApplyS:
    """``S = Q D`` with ``Q = I + H_alpha H_alpha^H`` formed once."""
    q = lk.q_hat(h_alpha)
    return lambda d: q @ d


def factored_s(h_alpha: np.ndarray) -> ApplyS:
    """``S = D + H_alpha (H_alpha^H D)``: two thin products, ``Q`` never formed."""
    hh = h_alpha.conj().T
    return lambda d: d + h_alpha @ (hh @ d)


#: The paper's per-column stop: ``cg_solve_complex`` leaves a column once ``r^H r < 1e-10``.
STOP_RS = 1e-10


def cg_iterates(
    apply_s: ApplyS, b: np.ndarray, t_max: int, stop: float = STOP_RS
) -> list[np.ndarray]:
    """Multi-RHS CG from ``X0 = 0``: the iterates ``X_1 .. X_t_max`` (the paper's recursion,
    ``salsa/salsa/utils.py`` ``cg_solve_complex``, fixed ``t``).  As there, a column stops
    moving once ``d^H s`` is zero (before its update) or ``r^H r < stop`` (after it)."""
    x = np.zeros_like(b, dtype=np.complex128)
    r = b.astype(np.complex128).copy()
    d = r.copy()
    rs = np.einsum("ij,ij->j", r.conj(), r).real
    live = np.ones(b.shape[1], dtype=bool)
    out = []
    for _ in range(int(t_max)):
        s = apply_s(d)
        ds = np.einsum("ij,ij->j", d.conj(), s).real
        live &= ds != 0
        omega = np.divide(rs, ds, out=np.zeros_like(rs), where=live)
        x = x + d * omega
        r = r - s * omega
        rs_new = np.einsum("ij,ij->j", r.conj(), r).real
        live &= ~(rs_new < stop)
        nu = np.divide(rs_new, rs, out=np.zeros_like(rs), where=live & (rs != 0))
        d = np.where(live, r + d * nu, 0)
        rs = np.where(live, rs_new, rs)
        out.append(x.copy())
    return out


def projection(a_i: np.ndarray, h_i: np.ndarray, r: int) -> np.ndarray:
    """``G_i`` (``r x N``) from ``A_i`` and the UE's SRS block ``H_i`` (the canonical form)."""
    m = h_i.conj().T @ a_i
    m = 0.5 * (m + m.conj().T)
    w, v = np.linalg.eigh(m)
    phi = v[:, np.argsort(w)[::-1][:r]]
    return (a_i @ phi).conj().T


@dataclass(frozen=True)
class Solve:
    """How the long-term solve is done."""

    form: str = "factored"  # "factored" | "explicit"
    regime: int = 1  # 1: B = own SRS blocks; 2: B = I


def sector_a(
    lt: lk.LongTerm, s: int, solve: Solve, t_max: int | None
) -> dict[int, list[np.ndarray]] | dict[int, np.ndarray]:
    """Per own UE of sector ``s``: ``A_i`` for every ``t`` (CG) or the exact one (``t_max`` None)."""
    ha, own = lt.h_alpha[s], lt.own[s]
    if own.size == 0:
        return {}
    n = ha.shape[0]
    b = lt.rhs[s] if solve.regime == 1 else np.eye(n, dtype=complex)
    if t_max is None:
        x = np.linalg.solve(lk.q_hat(ha), b)
        xs = [x]
    else:
        apply = factored_s(ha) if solve.form == "factored" else explicit_s(ha)
        xs = cg_iterates(apply, b, t_max)
    out: dict[int, list[np.ndarray]] = {}
    for k, u in enumerate(own):
        per_t = []
        for x in xs:
            if solve.regime == 1:
                a_i = x[:, k * lk.N_SRS : (k + 1) * lk.N_SRS]
            else:
                xsym = 0.5 * (x + x.conj().T)
                a_i = xsym @ lt.srs_own[int(u)]
            per_t.append(a_i)
        out[int(u)] = per_t if t_max is not None else per_t[0]
    return out


def sinr_lt_db(
    g: dict[int, np.ndarray],
    h_final: np.ndarray,
    pc_final: lk.PowerControl,
    served: np.ndarray,
) -> dict[int, float]:
    """Per served UE: ``10 log10`` of the frequency-mean LTBF SINR one ``T_LT`` later.

    Per sector, the projections of its UEs are stacked so ``G H_c`` is one product over every
    connected UE and frequency; per UE and frequency ``G Q G^H = G G^H + (G H_c)(G H_c)^H``.
    """
    out = {}
    conn = pc_final.iconn
    pos = {int(u): k for k, u in enumerate(conn)}
    by_sector: dict[int, list[int]] = {}
    for u in served:
        u = int(u)
        s = int(pc_final.gnb_idx[u])
        if s >= 0 and u in g:
            by_sector.setdefault(s, []).append(u)
    for s, ues in by_sector.items():
        hs = h_final[s][:, conn, :]  # (N, K, F)
        n, k, f = hs.shape
        gs = np.vstack([g[u] for u in ues])  # (sum r, N)
        gh_all = (gs @ hs.reshape(n, k * f)).reshape(-1, k, f)
        row = 0
        for u in ues:
            gi = g[u]
            r = gi.shape[0]
            gh = gh_all[row : row + r].transpose(2, 1, 0)  # (F, K, r)
            row += r
            qp = gi @ gi.conj().T + np.einsum(
                "fkr,fks->frs", gh, gh.conj()
            )  # (F, r, r)
            v = gh[:, pos[u], :][..., None]  # (F, r, 1)
            try:
                x = np.linalg.solve(qp, v)
            except (
                np.linalg.LinAlgError
            ):  # a degenerate G: a fixed-point A_i of rank < r
                x = np.linalg.pinv(qp, hermitian=True) @ v
            sv = np.real(np.sum(v.conj() * x, axis=(1, 2)))
            sinr = np.mean(sv / (1.0 - sv))
            out[u] = float(10.0 * np.log10(max(sinr, SINR_FLOOR)))
    return out


def sinr_ins_db(
    h_final: np.ndarray, pc_final: lk.PowerControl, served: np.ndarray
) -> dict[int, float]:
    """Instantaneous MMSE per served UE (Gram form), ``10 log10`` of the frequency mean."""
    out = {}
    conn = pc_final.iconn
    for u in served:
        u = int(u)
        s = int(pc_final.gnb_idx[u])
        if s < 0:
            continue
        k = int(np.where(conn == u)[0][0])
        hc = h_final[s][:, conn, :]  # (N, K, F)
        sinr = np.empty(hc.shape[2])
        for f in range(hc.shape[2]):
            gram = hc[:, :, f].conj().T @ hc[:, :, f]
            sm = gram @ np.linalg.inv(np.eye(gram.shape[0]) + gram)
            sv = np.real(sm[k, k])
            sinr[f] = sv / (1.0 - sv)
        out[u] = float(10.0 * np.log10(np.mean(sinr)))
    return out


@dataclass(frozen=True)
class DropEval:
    """What one drop gives for one configuration (per-UE SINR in dB)."""

    served: np.ndarray
    exact: dict[int, dict[int, float]]  # r -> per UE
    approx: dict[
        tuple[str, int, int, int], dict[int, float]
    ]  # (form, regime, t, r) -> per UE
    ins: dict[int, float]
    #: The fixed-point solves: (format name, form, regime, t, r) -> per UE.
    fixed: dict[tuple[str, str, int, int, int], dict[int, float]]


def _g_per_t(
    per_ue: dict[int, list[np.ndarray]],
    lt: lk.LongTerm,
    t_max: int,
    ranks: tuple[int, ...],
):
    """``(t, r) -> {u: G_u}`` from every UE's ``A_i`` iterates."""
    return {
        (t, r): {
            u: projection(a_t[t - 1], lt.srs_own[u], r) for u, a_t in per_ue.items()
        }
        for t in range(1, t_max + 1)
        for r in ranks
    }


def evaluate_channels(
    h0: np.ndarray,
    h1: np.ndarray,
    target: tuple[float, float],
    g_srs_db: float | None,
    rng: np.random.Generator,
    t_max: int = 10,
    ranks: tuple[int, ...] = (1, 2),
    solves: tuple[Solve, ...] = (Solve("factored", 1),),
    fixed: dict | None = None,
    fixed_solves: tuple[Solve, ...] = (),
) -> DropEval:
    """Exact, float CG and fixed-point CG LTBF SINR for every ``t <= t_max`` and rank.

    ``h0``, ``h1`` are the unscaled ``S x N x U x F`` channels of the two snapshots; ``fixed``
    maps a name to an :class:`~examples.ltbf_dse.ltbf_fixed.LtbfFormats`, each solved with every
    ``fixed_solves`` entry, or to ``(formats, solves)``."""
    from examples.ltbf_dse import ltbf_fixed as lf

    pc0 = lk.power_control(h0, target)
    pc1 = lk.power_control(h1, target)
    lt = lk.long_term(lk.scaled(h0, pc0), pc0, rng, g_srs_db)
    hf = lk.scaled(h1, pc1)
    served = np.intersect1d(pc0.iconn, pc1.iconn)
    exact_g: dict[int, dict[int, np.ndarray]] = {r: {} for r in ranks}
    for s in range(len(lt.own)):
        for u, a_i in sector_a(lt, s, Solve(), None).items():
            for r in ranks:
                exact_g[r][u] = projection(a_i, lt.srs_own[u], r)
    exact = {r: sinr_lt_db(exact_g[r], hf, pc1, served) for r in ranks}
    approx: dict[tuple[str, int, int, int], dict[int, float]] = {}
    for sv in solves:
        per_ue: dict[int, list[np.ndarray]] = {}
        for s in range(len(lt.own)):
            per_ue.update(sector_a(lt, s, sv, t_max))
        for (t, r), g in _g_per_t(per_ue, lt, t_max, ranks).items():
            approx[(sv.form, sv.regime, t, r)] = sinr_lt_db(g, hf, pc1, served)
    fx_out: dict[tuple[str, str, int, int, int], dict[int, float]] = {}
    for name, spec in (fixed or {}).items():
        # a name maps to formats (solved with every fixed_solves entry) or to (formats, solves)
        f, f_solves = spec if isinstance(spec, tuple) else (spec, fixed_solves)
        for sv in f_solves:
            per_ue = {}
            for s in range(len(lt.own)):
                per_ue.update(lf.sector_a_fixed(lt, s, f, t_max, sv.form, sv.regime))
            for (t, r), g in _g_per_t(per_ue, lt, t_max, ranks).items():
                fx_out[(name, sv.form, sv.regime, t, r)] = sinr_lt_db(
                    g, hf, pc1, served
                )
    return DropEval(served, exact, approx, sinr_ins_db(hf, pc1, served), fx_out)


def evaluate_drop(
    drop: dict,
    n: int,
    band: str,
    target: tuple[float, float],
    g_srs_db: float | None,
    rng: np.random.Generator,
    t_max: int = 10,
    ranks: tuple[int, ...] = (1, 2),
    solves: tuple[Solve, ...] = (Solve("factored", 1),),
    fixed: dict | None = None,
    fixed_solves: tuple[Solve, ...] = (),
) -> DropEval:
    """:func:`evaluate_channels` on one traced drop at array ``n x n`` over ``band``."""
    h0 = lk.channel(drop, 0, n, band)
    h1 = lk.channel(drop, 1, n, band)
    return evaluate_channels(
        h0, h1, target, g_srs_db, rng, t_max, ranks, solves, fixed, fixed_solves
    )
