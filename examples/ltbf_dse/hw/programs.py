"""programs.py -- the vector unit's two LTBF programs (spec §3, §5.3; spike 1.2 c).

``plans/ltbf_dse/ltbf_dse.md`` step 3.1.  Both are straight-line programs for
:class:`waveflow.vecunit.VecUnit`, assembled with :mod:`waveflow.vecunit.isa`, their formats a
parameter (the fixed-point configuration of ``ltbf_fixed_chain``):

* **the CG step**, one iteration a job, its state through Shared Mem.  In: ``X, R, D`` and the
  systolic product ``U`` (``Ĥα'(Ĥα'ᴴD)`` or ``K'D``, ``Ĥα' = 2^-s Ĥα``); out: ``X, R, D``.
  ``S = q_S(U + 2^-2s D)`` in one ``wf.vmacc.vs`` (a scalar 1 and the result shift ``2s``; ``S`` and
  ``U`` share the systolic ``c`` format, so loading ``U`` into ``S`` is exact); ``rz = |R|²`` is
  recomputed from the stored ``R`` (scalars cannot be loaded); then ``waveflow.linalg.cg.vec_step``'s
  steps 2-9 exactly (``ps``, ``alpha``, ``X``, ``R``, ``rz'``, ``beta``, ``D``).  A column's dot
  product spans all ``k = N_rx`` rows: a job is one block of ``k`` rows and ``n`` columns.
* **the equalizer**, ``x̂ = Σ_r conj(w_r) ⊙ z_r`` on tiles of ``U_pad`` streams x ``c`` resource
  elements (lanes are resource elements, elements are UEs): a ``vmul`` and, at r = 2, a ``vmacc``.

Each pass writes a group once and reads and writes one group a bank (the pass rules).
"""

from __future__ import annotations

from dataclasses import dataclass

from waveflow.utils.fixputils import Format
from waveflow.vecunit import isa as I
from waveflow.vecunit.asm import Programs
from waveflow.vecunit.config import VecUnitConfig

PORT = 0

# -- the CG step -------------------------------------------------------------------------------------

#: Input queues X, R, D, U; output queues X, R, D and (the last iteration) X_b.
QX, QR, QD, QU = 0, 1, 2, 3
OX, OR, OD, OXB = 0, 1, 2, 3
#: Groups a column group: X, R, D, S in banks 0-3, U in bank 0 again; 8 so NB = 4 divides NG.
GPG_CG = 8


#: The scalar holding 1 (``wf.vdotr`` of a vector of ones at VL 1): its format.
ONE = Format(16, 2, True)


@dataclass(frozen=True)
class CgStepFormats:
    """The CG step's formats: the registers (as ``linalg.cg.CgFormats``) and ``U``'s (systolic c)."""

    X: Format
    R: Format
    D: Format
    S: Format
    U: Format
    ps: Format
    rz: Format
    alpha: Format
    beta: Format
    g_div: int


def cg_step_config(
    f: CgStepFormats,
    k: int,
    n: int,
    L: int,
    word_bits: int = 128,
    lane_bits: int = 16,
    s_max: int = 4,
    m_max: int = 8,
) -> VecUnitConfig:
    """The CG step's unit: per column group ``X, R, D, S`` in banks 0-3, the ones vector ``E`` and
    ``X_b`` (amendment 1) in banks 0-1; ``rs_max`` covers ``2s`` and ``m``."""
    if f.S != f.U:
        raise ValueError(
            "S and U must share the systolic c format (U is loaded into S exactly)"
        )
    ng = -(-n // L)
    groups = (f.X, f.R, f.D, f.S, ONE, f.U, f.S, f.S)
    return VecUnitConfig(
        group_formats=groups * ng,
        scalar_formats=(f.ps, f.alpha, f.beta, f.rz, f.rz, ONE) * ng,
        in_queue_formats=(f.X, f.R, f.D, f.U),
        out_queue_formats=(f.X, f.R, f.D, f.U),
        L=L, VLMAX=k, Kmax=k, Nmax=n, NB=4, g_div=f.g_div, rs_max=max(2 * s_max, m_max),
        word_bits=word_bits, lane_bits=lane_bits,
    )  # fmt: skip


def cg_step_programs(
    cfg: VecUnitConfig, k: int, n: int, s: int = 0, last_m: int | None = None
) -> Programs:
    """One CG iteration on a ``k x n`` job with the sector's scale *s* (a program a scale); with
    *last_m*, the last iteration's variant that also stores ``X_b = q_U(2^-m X)`` (amendment 1).
    """
    G = cfg.groups_per_row(n)
    ex = []
    for g in range(G):
        E, one = GPG_CG * g + 4, 6 * g + 5
        ex += [I.setvl(1), I.vmv_v_i(E, 1)]
        ex += [I.vdotr(one, E, E)]
    ex += [I.setvl(k)]
    for g in range(G):
        b = GPG_CG * g
        X, R, D, S = b, b + 1, b + 2, b + 3
        ps, al, be, rz, rz2, one = (6 * g + j for j in range(6))
        ex += [I.vlse(R, QR, g, G).c, I.vdotr(rz, R, R)]
        ex += [I.vlse(D, QD, g, G).c, I.vlse(S, QU, g, G)]
        ex += [I.vmacc_vs(S, one, D, rs=2 * s).c, I.vdotr(ps, D, S)]
        ex += [I.vlse(X, QX, g, G)]
        ex += [I.sdivz(al, rz, ps)]
        ex += [I.vmacc_vs(X, al, D).c, I.vnmsac_vs(R, al, S).c, I.vdotr(rz2, R, R)]
        ex += [I.sdivz(be, rz2, rz)]
        ex += [I.vmadd_vs(D, be, R).c, I.vsse(OD, D, g, G)]
        ex += [I.vsse(OX, X, g, G), I.vsse(OR, R, g, G)]
        if last_m is not None:
            XB = GPG_CG * g + 5
            ex += [I.vmv_v_i(XB, 0)]
            ex += [I.vmacc_vs(XB, one, X, rs=last_m).c, I.vsse(OXB, XB, g, G)]
    outs = (OX, OR, OD) + ((OXB,) if last_m is not None else ())
    ex += [I.qpop(q) for q in (QX, QR, QD, QU)] + [I.qpush(q) for q in outs]
    load = [I.ld(q, PORT, k, n) for q in (QX, QR, QD, QU)]
    store = [I.st(PORT, q, q, k, n) for q in outs]
    return Programs(load=load, execute=ex, store=store)


# -- the equalizer -----------------------------------------------------------------------------------

#: Input queues W0, Z0, W1, Z1 (each ``U_pad x c``); output X.
QW0, QZ0, QW1, QZ1 = 0, 1, 2, 3
OXH = 0


@dataclass(frozen=True)
class EqFormats:
    w: Format  # the estimator's weights
    z: Format  # the projection (systolic c)
    y: Format  # the accumulator group
    xh: Format  # the output


def eq_config(
    f: EqFormats,
    u_pad: int,
    c: int,
    L: int,
    r: int,
    word_bits: int = 128,
    lane_bits: int = 16,
) -> VecUnitConfig:
    G = -(-c // L)
    return VecUnitConfig(
        group_formats=(f.w, f.z, f.y, f.w, f.z, f.y, f.w, f.z) * G,
        scalar_formats=(f.y,),
        in_queue_formats=(f.w, f.z, f.w, f.z)[: 2 * r],
        out_queue_formats=(f.xh,),
        L=L, VLMAX=u_pad, Kmax=u_pad, Nmax=c, NB=4, rs_max=0,
        word_bits=word_bits, lane_bits=lane_bits,
    )  # fmt: skip


def eq_programs(cfg: VecUnitConfig, u_pad: int, c: int, r: int, tiles: int,
                off: Offsets | None = None) -> Programs:  # fmt: skip
    """``x = Σ_r conj(w_r) z_r`` on *tiles* tiles (one load of each ``w_r``, ``z_r`` a tile); *off*
    places its groups and queues after the CG step's in a combined unit (:func:`system_config`).
    """
    o = off or Offsets()
    QW0, QZ0, QW1, QZ1 = (o.qi + q for q in (0, 1, 2, 3))
    OXH = o.qo
    G = cfg.groups_per_row(c)
    ex = [I.setvl(u_pad)]
    for _ in range(tiles):
        for g in range(G):
            b = o.g + 8 * g
            W0, Z0, Y, W1, Z1 = b, b + 1, b + 2, b + 3, b + 4
            ex += [I.vlse(W0, QW0, g, G).c, I.vlse(Z0, QZ0, g, G).c]
            ex += [I.vmul_vv(Y, W0, Z0, conj=True)]
            if r == 2:
                ex += [I.vlse(W1, QW1, g, G).c, I.vlse(Z1, QZ1, g, G).c]
                ex += [I.vmacc_vv(Y, W1, Z1, conj=True)]
            ex += [I.vsse(OXH, Y, g, G)]
        ex += [I.qpop(q) for q in (QW0, QZ0, QW1, QZ1)[: 2 * r]] + [I.qpush(OXH)]
    ins = (QW0, QZ0, QW1, QZ1)[: 2 * r]
    load = [I.ld(q, PORT, u_pad, c) for _ in range(tiles) for q in ins]
    store = [I.st(PORT, OXH, t, u_pad, c) for t in range(tiles)]
    return Programs(load=load, execute=ex, store=store)


# -- one unit for both -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Offsets:
    """Where the equalizer's groups, input and output queues start in a combined unit."""

    g: int = 0
    qi: int = 0
    qo: int = 0


def system_config(cg: CgStepFormats, eq: EqFormats, *, k: int, n: int, u_pad: int, r: int,
                  L: int, word_bits: int = 128, lane_bits: int = 16, s_max: int = 4,
                  m_max: int = 8) -> tuple[VecUnitConfig, Offsets]:  # fmt: skip
    """One vector unit that runs both programs (a build has one format a register): the CG step's
    groups, scalars and queues first (its programs unchanged), the equalizer's after them (its
    programs with the returned :class:`Offsets`)."""
    a = cg_step_config(cg, k, n, L, word_bits, lane_bits, s_max, m_max)
    b = eq_config(eq, u_pad, n, L, r, word_bits, lane_bits)
    cfg = VecUnitConfig(
        group_formats=a.group_formats + b.group_formats,
        scalar_formats=a.scalar_formats + b.scalar_formats,
        in_queue_formats=a.in_queue_formats + b.in_queue_formats,
        out_queue_formats=a.out_queue_formats + b.out_queue_formats,
        L=L, VLMAX=max(a.VLMAX, b.VLMAX), Kmax=max(a.Kmax, b.Kmax), Nmax=max(a.Nmax, b.Nmax),
        NB=4, g_div=a.g_div, rs_max=a.rs_max, word_bits=word_bits, lane_bits=lane_bits,
    )  # fmt: skip
    return cfg, Offsets(
        len(a.group_formats), len(a.in_queue_formats), len(a.out_queue_formats)
    )
