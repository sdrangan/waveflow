"""Fixed-point conjugate gradient as a program for the SIMD core (``plans/simdcore.md`` Stage 3).

The problem of ``examples/vecunit_cg``: ``A X = B`` for a Hermitian positive definite ``A`` (``K x K``)
and ``N`` right-hand sides (``K x N``), every column its own CG with its own ``alpha`` and ``beta``; the
matrix multiply ``S = A P`` is outside the core (the systolic array), the core does steps 2-9.

**Mapping: a register is a row.**  Row ``k`` of a ``K x N`` matrix is one register of ``VL = N``
complex elements (``e32``), so a column's dot product ``Σ_k Re(conj(P_k) S_k)`` is an elementwise
accumulation over ``K`` registers (``wf.vcdotra``) -- no segmented reduction -- and ``alpha``, ``beta``
are vectors of ``N``, divided by ``vdiv`` (the ``vdiv`` build option, records D6)::

    v0        mask (the divide guards)       v8 .. v15   window IN:  B, then each S (two copies)
    v1 ps  v2 alpha  v3 rz  v4 rz'  v5 beta    v16 .. v23  window OUT: P, one copy (it stays P)
    v6 v7     temporaries                    v24 .. v31  R
    X         in data memory, row k at 4·N·k (8 vle / vse an iteration: 32 registers hold IN, P, R)

**Formats** (16-bit complex vectors): ``B``, ``R``, ``P`` Q4.12; ``S`` Q5.11; ``X`` Q3.13; ``A`` Q3.13
(the multiply's); ``ps`` and ``rz`` frac 14 (32-bit); ``alpha`` frac 11, ``beta`` frac 13 (16-bit).
Rounding half up (``vxrm`` 0), saturating.  The shifts follow from them (``RSH_*``, ``G_*``,
``CSH_*`` below).  The integer golden (:func:`cg_golden`) follows the program's arithmetic
step by step -- per-term rounding in the dot products, ``vdiv``'s truncation -- so it is a **new
golden**, not ``waveflow.linalg.cg``'s; its accuracy is compared with that one's (criterion 2).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from waveflow.linalg import cg
from waveflow.utils.fixputils import Format, OMode, QMode
from waveflow.simdcore.config import SimdCoreConfig, Window
from waveflow.simdcore.model import JobResult, cpack, cunpack, run_job

V_MASK, V_PS, V_ALPHA, V_RZ, V_RZ2, V_BETA, V_T0, V_T1 = range(8)
V_IN, V_P, V_R = 8, 16, 24
Q_IN, Q_OUT = 0, 1

F_B = F_R = F_P = 12
F_S, F_X, F_A = 11, 13, 13
#: The scalars use the room 32 and 16 bits give (Stage 3 record: frac 10 / 11 / 7 / 9, the centre
#: build's, left this core's CG 1.5x less accurate than the vector unit's).  The dividends' headroom:
#: ``rz << G_ALPHA`` needs ``rz < 2**(31 - G_ALPHA - F_RZ)`` = 64, ``rz' << G_BETA`` ``< 16``.
F_PS, F_RZ, F_ALPHA, F_BETA = 14, 14, 11, 13
RSH_PS = F_P + F_S - F_PS  # 9: Re(conj P S) frac 23 -> ps frac 14
RSH_RZ = F_R + F_R - F_RZ  # 10: |R|^2 frac 24 -> rz frac 14
G_ALPHA = F_ALPHA + F_PS - F_RZ  # 11: (rz << 11) / ps -> alpha frac 11
G_BETA = F_BETA  # 13: (rz' << 13) / rz -> beta frac 13
CSH_ALPHA = F_P + F_ALPHA - F_X  # 10: P x alpha frac 23 -> X frac 13 (and S x alpha 22 -> R 12)
assert F_S + F_ALPHA - F_R == CSH_ALPHA
CSH_BETA = F_P + F_BETA - F_P  # 13


def cg_config(K: int = 8, N: int = 32, *, L: int = 2, vlen: int = 2048, **kw) -> SimdCoreConfig:
    if not (1 <= K <= 8 and 1 <= N <= vlen // 32):
        raise ValueError(f"K = {K} (1..8), N = {N} (1..{vlen // 32})")
    return SimdCoreConfig(L=L, vlen=vlen, vdiv=True, dm_bytes=max(4096, 4 * -(-N // 16) * 16 * K * 2), windows=(
        Window("IN", V_IN, K, "in"), Window("OUT", V_P, K, "out", copies=1)), **kw)


def _wfsh(csh: int, rsh: int = 0) -> str:
    return f"    li t2, {(rsh << 8) | csh}\n    csrw wfsh, t2\n"


def _div(dst: str, num: str, den: str, g: int) -> str:
    """``dst = den == 0 ? 0 : sat16((num << g) / den)``, then as a complex scalar (``im`` = 0)."""
    return (f"    vsll.vi v{V_T0}, {num}, {g}\n"
            f"    vdiv.vv {dst}, v{V_T0}, {den}\n"
            f"    vmseq.vi v{V_MASK}, {den}, 0\n"
            f"    vmerge.vim {dst}, {dst}, 0, v{V_MASK}\n"
            f"    vmin.vx {dst}, {dst}, s1\n    vmax.vx {dst}, {dst}, s2\n"
            f"    vand.vx {dst}, {dst}, s3\n")


def cg_text(K: int, nit: int, *, skip_dead: bool = True) -> str:
    """One job: ``B`` in, ``nit`` iterations (``S`` in, ``P`` out each but the last), ``X`` out.
    ``a0`` = ``N``."""
    t = ["    vsetvli t0, a0, e32, m1, ta, ma", "    li s1, 32767", "    li s2, -32768", "    li s3, 0xFFFF",
         "    addi s4, a0, 15", "    andi s4, s4, -16",  # s4 = a row's bytes in DM (X): N rounded
         "    slli s4, s4, 2"]  # up to 16 elements, so every row starts on a bank boundary (L <= 16, D13)
    t.append(f"    wf.qpop {Q_IN}")  # B
    t += [f"    vmv.v.v v{V_R + k}, v{V_IN + k}" for k in range(K)]  # R = B
    t += [f"    vmv.v.v v{V_P + k}, v{V_R + k}" for k in range(K)]  # P = R
    t += ["    li t1, 0"] + [f"    vse32.v v{V_T1}, (t1)\n    add t1, t1, s4" for _ in range(K)]
    t[-K:] = [f"    vmv.v.i v{V_T1}, 0"] + t[-K:]  # X = 0
    t.append(_wfsh(0, RSH_RZ).rstrip())
    rz, rz2 = V_RZ, V_RZ2
    t += [f"    vmv.v.i v{rz}, 0"] + [f"    wf.vcdotra.vv v{rz}, v{V_R + k}, v{V_R + k}" for k in range(K)]
    t.append(f"    wf.qpush {Q_OUT}")  # P0 -> the multiply
    for it in range(1, nit + 1):
        last = it == nit
        t.append(f"    wf.qpop {Q_IN}")  # S = A P
        t.append(_wfsh(0, RSH_PS).rstrip())
        t += [f"    vmv.v.i v{V_PS}, 0"] + [f"    wf.vcdotra.vv v{V_PS}, v{V_P + k}, v{V_IN + k}" for k in range(K)]
        t.append(_div(f"v{V_ALPHA}", f"v{rz}", f"v{V_PS}", G_ALPHA).rstrip())
        t.append(_wfsh(CSH_ALPHA).rstrip())
        t.append("    li t1, 0")
        for k in range(K):  # X += P alpha
            t += [f"    vle32.v v{V_T1}, (t1)", f"    wf.vcmacc.vv v{V_T1}, v{V_P + k}, v{V_ALPHA}",
                  f"    vse32.v v{V_T1}, (t1)", "    add t1, t1, s4"]
        t += [f"    vrsub.vx v{V_T0}, v{V_ALPHA}, zero", f"    vand.vx v{V_T0}, v{V_T0}, s3"]  # -alpha
        t += [f"    wf.vcmacc.vv v{V_R + k}, v{V_IN + k}, v{V_T0}" for k in range(K)]  # R -= S alpha
        if last and skip_dead:
            break
        t.append(_wfsh(0, RSH_RZ).rstrip())
        t += [f"    vmv.v.i v{rz2}, 0"] + [f"    wf.vcdotra.vv v{rz2}, v{V_R + k}, v{V_R + k}" for k in range(K)]
        t.append(_div(f"v{V_BETA}", f"v{rz2}", f"v{rz}", G_BETA).rstrip())
        t.append(_wfsh(CSH_BETA).rstrip())
        t += [f"    wf.vcmadd.vv v{V_P + k}, v{V_R + k}, v{V_BETA}" for k in range(K)]  # P = R + P beta
        rz, rz2 = rz2, rz  # rz = rz' by renaming
        if not last:
            t.append(f"    wf.qpush {Q_OUT}")
    t.append("    li t1, 0")
    for k in range(K):  # X -> OUT
        t += [f"    vle32.v v{V_P + k}, (t1)", "    add t1, t1, s4"]
    t += [f"    wf.qpush {Q_OUT}", "    ecall"]
    return "\n".join(t) + "\n"


# --- the integer golden ------------------------------------------------------------------------------

def _rnu(p, d):
    p = np.asarray(p, dtype=np.int64)
    return p if d == 0 else (p + (1 << (d - 1))) >> d


def _s(v, bits):
    return np.clip(v, -(1 << (bits - 1)), (1 << (bits - 1)) - 1)


def _div0(num, den):
    """``vdiv`` per element: toward zero; the guard makes ``den == 0`` give 0."""
    num, den = np.asarray(num, np.int64), np.asarray(den, np.int64)
    q = np.where(den == 0, 0, np.abs(num) // np.where(den == 0, 1, np.abs(den)))
    return np.where((num < 0) != (den < 0), -q, q)


def mm_step(ar, ai, pr, pi):
    """``S = sat16(rnu(A @ P >> 14))``: A Q3.13 x P Q4.12 (frac 25) -> S Q5.11, exact before the round."""
    sr = ar @ pr - ai @ pi
    si = ar @ pi + ai @ pr
    sh = F_A + F_P - F_S
    return _s(_rnu(sr, sh), 16), _s(_rnu(si, sh), 16)


@dataclass
class CgResult:
    xr: np.ndarray
    xi: np.ndarray
    ps: list
    rz: list


def cg_golden(ar, ai, br, bi, nit: int, *, skip_dead: bool = True) -> CgResult:
    """The program's arithmetic in NumPy, step by step (``K x N`` stored integers)."""
    br, bi = np.asarray(br, np.int64), np.asarray(bi, np.int64)
    rr, ri = br.copy(), bi.copy()
    pr, pi = rr.copy(), ri.copy()
    xr, xi = np.zeros_like(rr), np.zeros_like(ri)

    def dot(a_r, a_i, b_r, b_i, rsh):  # Σ_k sat32(rnu(Re(conj a) b, rsh)), saturating accumulation
        acc = np.zeros(a_r.shape[1], np.int64)
        for k in range(a_r.shape[0]):
            acc = _s(acc + _s(_rnu(a_r[k] * b_r[k] + a_i[k] * b_i[k], rsh), 32), 32)
        return acc

    def scal(num, den, g):
        return _s(_div0(num << g, den), 16)

    def cmac(xr_, xi_, ar_, ai_, s, csh):  # x + sat16(rnu(a * s)), s real
        return _s(xr_ + _s(_rnu(ar_ * s, csh), 16), 16), _s(xi_ + _s(_rnu(ai_ * s, csh), 16), 16)

    rz = dot(rr, ri, rr, ri, RSH_RZ)
    pss, rzs = [], [rz]
    for it in range(1, nit + 1):
        sr, si = mm_step(ar, ai, pr, pi)
        ps = dot(pr, pi, sr, si, RSH_PS)
        pss.append(ps)
        alpha = scal(rz, ps, G_ALPHA)
        xr, xi = cmac(xr, xi, pr, pi, alpha, CSH_ALPHA)
        rr, ri = cmac(rr, ri, sr, si, -alpha, CSH_ALPHA)
        if it == nit and skip_dead:
            break
        rz2 = dot(rr, ri, rr, ri, RSH_RZ)
        rzs.append(rz2)
        beta = scal(rz2, rz, G_BETA)
        # P = sat16(sat16(rnu(P beta)) + R)
        pr = _s(_s(_rnu(pr * beta, CSH_BETA), 16) + rr, 16)
        pi = _s(_s(_rnu(pi * beta, CSH_BETA), 16) + ri, 16)
        rz = rz2
    return CgResult(xr, xi, pss, rzs)


# --- the harness -------------------------------------------------------------------------------------

def _q(fmt_bits: int, int_bits: int) -> Format:
    return Format(fmt_bits, int_bits, True, QMode.AP_RND, OMode.AP_SAT)


#: Formats for :func:`problem` (``cg_unit.problem`` quantizes ``A`` and ``B`` with ``f.A``, ``f.B``).
FORMATS = cg.CgFormats(A=_q(16, 3), B=_q(16, 4), P=_q(16, 4), R=_q(16, 4), S=_q(16, 5), X=_q(16, 3),
                       ps=_q(32, 22), rz=_q(32, 21), alpha=_q(16, 9), beta=_q(16, 7), g_div=6)


def problem(K: int, N: int, seed: int):
    """``examples/vecunit_cg``'s construction (a random HPD ``A`` and a ``B``), quantized to Q3.13 / Q4.12."""
    from examples.vecunit_cg.cg_unit import problem as vu_problem

    return vu_problem(FORMATS, K, N, seed)


def run_cg(cfg: SimdCoreConfig, ar, ai, br, bi, nit: int, *, skip_dead: bool = True) -> tuple[CgResult, JobResult]:
    """One solve on the interpreter, closed loop: each ``P`` the core pushes comes back as ``S = A P``."""
    K, N = np.shape(br)

    def systolic(window, core):
        if window != "IN" or not core.outq["OUT"]:
            return None
        p = core.outq["OUT"][-1]  # N x K
        pr, pi = cunpack(p)
        sr, si = mm_step(np.asarray(ar), np.asarray(ai), pr.T, pi.T)
        return cpack(sr.T, si.T)

    inputs = {"IN": [cpack(np.asarray(br).T, np.asarray(bi).T)]}
    r = run_job(cfg, cg_text(K, nit, skip_dead=skip_dead), inputs=inputs, args=[N], on_empty=systolic).check()
    xr, xi = cunpack(r.outputs["OUT"][-1])
    return CgResult(xr.T, xi.T, [], []), r


def cg_job(cfg: SimdCoreConfig, ar, ai, br, bi, nit: int, tx_id: int):
    """One solve as a pysim job: its inputs are ``B`` then the ``S`` blocks a closed-loop interpreter
    run fed it -- the static stimulus of ``examples/vecunit_cg``'s bench."""
    from waveflow.simdcore.unit import Job

    K, N = np.shape(br)
    fed: list = [cpack(np.asarray(br).T, np.asarray(bi).T)]

    def systolic(window, core):
        if window != "IN" or not core.outq["OUT"]:
            return None
        pr, pi = cunpack(core.outq["OUT"][-1])
        sr, si = mm_step(np.asarray(ar), np.asarray(ai), pr.T, pi.T)
        fed.append(cpack(sr.T, si.T))
        return fed[-1]

    run_job(cfg, cg_text(K, nit), inputs={"IN": [fed[0]]}, args=[N], on_empty=systolic).check()
    return Job(tx_id, cg_text(K, nit), args=(N,), inputs={"IN": fed})


def float_problem(K: int, N: int, seed: int):
    """The unquantized ``A``, ``B`` of :func:`problem` (the same construction and draws)."""
    rng = np.random.default_rng(seed)
    m = 8 * K
    H = (rng.standard_normal((m, K)) + 1j * rng.standard_normal((m, K))) / np.sqrt(2)
    Y = (rng.standard_normal((m, N)) + 1j * rng.standard_normal((m, N))) / np.sqrt(2)
    return (H.conj().T @ H + 0.1 * np.eye(K)) / m, H.conj().T @ Y / m


def float_cg(A, B, nit: int):
    X = np.zeros_like(B)
    R, P = B.copy(), B.copy()
    rz = np.sum(np.abs(R) ** 2, 0)
    for _ in range(nit):
        S = A @ P
        alpha = rz / np.real(np.sum(P.conj() * S, 0))
        X, R = X + P * alpha, R - S * alpha
        rz2 = np.sum(np.abs(R) ** 2, 0)
        P, rz = R + P * (rz2 / rz), rz2
    return X


def accuracy(K: int = 8, N: int = 32, nit: int = 3, seeds=range(8)) -> dict[str, float]:
    """Mean relative residual ``‖B − A X‖ / ‖B‖`` on the float problem after ``nit`` iterations:
    float CG, this core's integer CG (:func:`cg_golden`), and ``examples/vecunit_cg``'s centre build
    (``waveflow.linalg.cg.cg_solve``) -- success criterion 2."""
    from examples.vecunit_cg.cg_unit import CENTRE
    from examples.vecunit_cg.cg_unit import problem as vu_problem

    out = {"float": [], "simdcore": [], "vecunit_centre": []}
    for seed in seeds:
        A, B = float_problem(K, N, seed)

        def res(X):
            return float(np.linalg.norm(B - A @ X) / np.linalg.norm(B))

        out["float"].append(res(float_cg(A, B, nit)))
        g = cg_golden(*problem(K, N, seed), nit)
        out["simdcore"].append(res((g.xr + 1j * g.xi) / 2**F_X))
        st = cg.cg_solve(*vu_problem(CENTRE, K, N, seed), nit, CENTRE)
        out["vecunit_centre"].append(res((st.xr + 1j * st.xi) / 2**CENTRE.X.frac_bits))
    return {k: float(np.mean(v)) for k, v in out.items()}
