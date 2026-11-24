"""cg_golden.py -- the bit-exact golden of the CG worker (``plans/dse_matrix_sched.md`` Section 7a, E0).

A worker solves ``(I + H Hᴴ) X = B`` by ``t`` iterations of conjugate gradient from ``X₀ = 0``, in the
**factored form**: ``S = D + H (Hᴴ D)``, never forming ``I + H Hᴴ``.  ``H`` is ``N_rx x m`` and ``B``
``N_rx x c``; every column has its own ``ω`` and ``ν``.  The engine knows nothing of users or whitening.

The arithmetic is :mod:`waveflow.linalg.cg`'s (``cg_init``, ``vec_step``: steps 2-9 of its docstring,
with ``P`` the search direction ``D``); only step 1 is replaced, by the factored product::

    T = q_T(Hᴴ D)            exact sum over N_rx, then rounded once
    U = H T                  exact sum over m
    S = q_S(D + U)           exact add, then rounded once

**The stop rule** (E0, the paper's code): a column stops for good once ``dᴴs ≤ 0`` (in exact arithmetic
``dᴴs > 0``; a non-positive value is quantization breaking down) or ``‖r‖² ≤ rz_floor`` (``rz_floor`` a
stored integer of ``rz``'s format; ``0`` means below one LSB).  A stopped column keeps its ``X``, ``R``,
``D`` and ``rz``.  The test is made on the state an iteration starts from (``rz``) and on that iteration's
``ps``, before any update.

The result is ``X`` rounded to the output format ``Xo`` (16-bit, as the inputs), so four complex samples
travel in a 128-bit word.  :func:`float_solve` is the same CG in complex128, for the quantization error.

Representation as in :mod:`waveflow.linalg.cg`: ``(re, im)`` pairs of int64 stored integers.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from waveflow.linalg import cg
from waveflow.linalg.matmul import acc_format, matmul_exact
from waveflow.utils import fixputils as fx
from waveflow.utils.fixputils import Format, OMode, QMode

__all__ = ["reg", "CgWorkerFormats", "FORMATS", "Problem", "make_problem", "fixed_solve",
           "float_solve", "quant_error", "N_RX", "M_MAX", "C_MAX", "T_MAX", "COLS_PER_USER"]

# --- E0 sizes (decided 2026-11-22, plans/dse_matrix_sched.md Section 9 item 3) -------------------
N_RX = 64            #: receive antennas: the rows of H, B and X
COLS_PER_USER = 8    #: SRS columns a user
M_MAX = 32           #: columns of H, at most 4 users a set
C_MAX = 8            #: right-hand sides
T_MAX = 16           #: the largest iteration count a job may ask (E0's t = 4)


def reg(width: int, int_bits: int) -> Format:
    """A register: round half up, saturate (``ap_fixed<width, int_bits, AP_RND, AP_SAT>``)."""
    return Format(width, int_bits, True, QMode.AP_RND, OMode.AP_SAT)


@dataclass(frozen=True)
class CgWorkerFormats:
    """The worker's formats: :class:`waveflow.linalg.cg.CgFormats` (``A`` unused -- there is no ``A``;
    ``P`` is the direction ``D``) plus ``H``, the factored product's ``T``, the output ``Xo`` and the
    stop floor."""

    cg: cg.CgFormats
    H: Format
    T: Format
    Xo: Format
    rz_floor: int = 0

    # Exact intermediates (all must fit 64 bits; the HLS accumulators are these types).
    def acc_T(self, n_rx: int = N_RX) -> Format:
        return acc_format(self.H, self.cg.P, n_rx)

    def acc_U(self, m: int = M_MAX) -> Format:
        return acc_format(self.H, self.T, m)

    def acc_S(self, m: int = M_MAX) -> Format:
        return fx.add_format(self.cg.P, self.acc_U(m))

    def check(self) -> None:
        """Fail fast if an intermediate exceeds 64 bits (the HLS and numpy guard alike)."""
        self.acc_T(), self.acc_S()
        self.cg.intermediates(N_RX)


#: The E1 formats.  Inputs Q4.12 (16-bit, the job stream's samples); vectors 18-bit (one DSP48 port);
#: ``T`` and ``S`` 24-bit (the other port, 25 bits); scalars 32-bit.  Chosen from the ranges of
#: :func:`make_problem`'s channels (``python -m examples.cg_pool.cg_golden``).
FORMATS = CgWorkerFormats(
    cg=cg.CgFormats(A=reg(16, 4), B=reg(16, 4), P=reg(18, 4), R=reg(18, 4), S=reg(24, 13),
                    X=reg(18, -1), ps=reg(32, 19), rz=reg(32, 12), alpha=reg(18, -2),
                    beta=reg(18, 3), g_div=8),
    H=reg(16, 4), T=reg(24, 11), Xo=reg(16, -1),
)


# --- one job's data ---------------------------------------------------------------------------------
@dataclass
class Problem:
    """One job: its header fields and its stored-integer ``H`` (``N_rx x m``) and ``B`` (``N_rx x c``)."""

    tx_id: int
    m: int
    c: int
    t: int
    hr: np.ndarray
    hi: np.ndarray
    br: np.ndarray
    bi: np.ndarray
    meta: dict = field(default_factory=dict)


def make_problem(rng: np.random.Generator, tx_id: int, *, users: int | None = None, c: int = C_MAX,
                 t: int = 4, snr_db: tuple[float, float] = (-5.0, 5.0), rho: float = 0.5,
                 f: CgWorkerFormats = FORMATS) -> Problem:
    """A synthetic job: ``users`` (1-4, random if ``None``) users of 8 columns each, entries of user
    ``j`` complex Gaussian with power ``snr_j`` an antenna (``snr_db`` uniform), consecutive users
    correlated by ``rho`` (a shared component), and ``B`` the first user's columns -- LTBF's
    ``Q̂ Aᵢ = Ĥᵢ`` with user ``i`` in the set.  Quantized to ``H`` and ``B``'s formats."""
    k = int(users) if users is not None else int(rng.integers(1, 5))
    cols = []
    prev = None
    for j in range(k):
        g = (rng.standard_normal((N_RX, COLS_PER_USER))
             + 1j * rng.standard_normal((N_RX, COLS_PER_USER))) / np.sqrt(2)
        if prev is not None and rho:
            g = np.sqrt(rho) * prev + np.sqrt(1 - rho) * g
        prev = g
        snr = 10 ** (rng.uniform(*snr_db) / 10)
        cols.append(np.sqrt(snr) * g)
    H = np.concatenate(cols, axis=1)
    B = H[:, :c]
    q = fx.quantize_real
    return Problem(tx_id, H.shape[1], int(c), int(t), q(H.real, f.H), q(H.imag, f.H),
                   q(B.real, f.cg.B), q(B.imag, f.cg.B), {"users": k})


# --- the fixed-point solve --------------------------------------------------------------------------
def factored_s(hr, hi, dr, di, f: CgWorkerFormats):
    """``S = q_S(D + H q_T(Hᴴ D))``: the worker's matrix step, exact before each rounding."""
    tr, ti, t_fmt = matmul_exact(hr, hi, f.H, dr, di, f.cg.P, adjoint=True)
    tr, ti = fx.quantize(tr, t_fmt, f.T), fx.quantize(ti, t_fmt, f.T)
    ur, ui, u_fmt = matmul_exact(hr, hi, f.H, tr, ti, f.T)
    sr, s_fmt = fx.add(dr, f.cg.P, ur, u_fmt)
    si, _ = fx.add(di, f.cg.P, ui, u_fmt)
    return fx.quantize(sr, s_fmt, f.cg.S), fx.quantize(si, s_fmt, f.cg.S)


def fixed_solve(p: Problem, f: CgWorkerFormats = FORMATS, *, trace: list | None = None):
    """The worker's result for job ``p``: ``(xr, xi)`` in ``Xo``, shape ``N_rx x c``, and the
    iterations each column ran (its stop rule).  ``trace`` collects each iteration's state."""
    g = f.cg
    state = cg.cg_init(p.br, p.bi, g)
    stopped = np.zeros((1, p.c), dtype=bool)
    ran = np.zeros(p.c, dtype=np.int64)
    for _ in range(int(p.t)):
        stopped |= state.rz <= f.rz_floor
        sr, si = factored_s(p.hr, p.hi, state.pr, state.pi, f)
        new, scal = cg.vec_step(state, sr, si, g)
        stopped |= scal["ps"] <= 0
        keep = stopped                                     # (1, c): broadcasts over rows
        state = cg.CgState(*(np.where(keep, old, nw) for old, nw in zip(
            (state.xr, state.xi, state.rr, state.ri, state.pr, state.pi, state.rz),
            (new.xr, new.xi, new.rr, new.ri, new.pr, new.pi, new.rz))))
        ran += ~stopped[0]
        if trace is not None:
            trace.append({"S": (sr, si), **scal, "state": state, "stopped": stopped.copy()})
    xr, xi = fx.quantize(state.xr, g.X, f.Xo), fx.quantize(state.xi, g.X, f.Xo)
    return xr, xi, ran


def float_solve(p: Problem, f: CgWorkerFormats = FORMATS) -> np.ndarray:
    """The same ``t`` iterations of CG in complex128 on the quantized inputs (the reference for the
    quantization error), without the fixed-point stop rule."""
    H = fx.to_float(p.hr, f.H) + 1j * fx.to_float(p.hi, f.H)
    B = fx.to_float(p.br, f.cg.B) + 1j * fx.to_float(p.bi, f.cg.B)
    X = np.zeros_like(B)
    R = B.copy()
    D = R.copy()
    rz = np.sum(np.abs(R) ** 2, axis=0)
    for _ in range(int(p.t)):
        S = D + H @ (H.conj().T @ D)
        ps = np.real(np.sum(D.conj() * S, axis=0))
        w = np.where(ps > 0, rz / np.where(ps > 0, ps, 1), 0.0)
        X = X + D * w
        R = R - S * w
        rz2 = np.sum(np.abs(R) ** 2, axis=0)
        nu = np.where(rz > 0, rz2 / np.where(rz > 0, rz, 1), 0.0)
        D = R + D * nu
        rz = rz2
    return X


def quant_error(p: Problem, f: CgWorkerFormats = FORMATS) -> float:
    """The fixed-point result's error against :func:`float_solve`, relative, in dB
    (``10 log10 ‖X_fixed - X_float‖² / ‖X_float‖²``)."""
    xr, xi, _ = fixed_solve(p, f)
    xf = fx.to_float(xr, f.Xo) + 1j * fx.to_float(xi, f.Xo)
    ref = float_solve(p, f)
    return float(10 * np.log10(np.sum(np.abs(xf - ref) ** 2) / np.sum(np.abs(ref) ** 2)))


def ranges(n_jobs: int = 200, seed: int = 0, f: CgWorkerFormats = FORMATS) -> dict[str, float]:
    """The largest magnitude each register takes, in float, over ``n_jobs`` synthetic jobs (from the
    float solve's intermediates): the basis for :data:`FORMATS`'s integer bits."""
    rng = np.random.default_rng(seed)
    out: dict[str, float] = {}

    def see(k, v):
        out[k] = max(out.get(k, 0.0), float(np.max(np.abs(v))))

    for i in range(n_jobs):
        p = make_problem(rng, i, f=f)
        H = fx.to_float(p.hr, f.H) + 1j * fx.to_float(p.hi, f.H)
        B = fx.to_float(p.br, f.cg.B) + 1j * fx.to_float(p.bi, f.cg.B)
        X, R, D = np.zeros_like(B), B.copy(), B.copy()
        rz = np.sum(np.abs(R) ** 2, axis=0)
        see("H", H), see("B", B), see("rz", rz)
        for _ in range(p.t):
            T = H.conj().T @ D
            S = D + H @ T
            ps = np.real(np.sum(D.conj() * S, axis=0))
            w = rz / ps
            X, R = X + D * w, R - S * w
            rz2 = np.sum(np.abs(R) ** 2, axis=0)
            nu = rz2 / rz
            D, rz = R + D * nu, rz2
            for k, v in (("T", T), ("S", S), ("ps", ps), ("alpha", w), ("X", X), ("R", R),
                         ("D", D), ("rz", rz), ("beta", nu)):
                see(k, v)
        out["min_alpha"] = min(out.get("min_alpha", np.inf), float(np.min(np.abs(w))))
    return out


if __name__ == "__main__":
    FORMATS.check()
    for k, v in ranges().items():
        print(f"{k:10s} {v:12.5g}  bits {int(np.ceil(np.log2(max(v, 1e-30)))) + 1:+d}")
    rng = np.random.default_rng(1)
    errs = [quant_error(make_problem(rng, i)) for i in range(50)]
    print(f"quantization error: mean {np.mean(errs):.1f} dB, worst {np.max(errs):.1f} dB")
