"""CG as a program for the vector unit (``plans/vecunit_new.md``): the vector half of
``waveflow.linalg.cg``, steps 2-9, bit for bit -- the unit itself knows nothing about CG.

**The configuration** (:func:`cg_config`) comes from a ``CgFormats``.  Per column group ``g`` (``ng =
ceil(n / L)`` of them, ``L`` columns each, every column its own CG problem): four vector groups
``X_g, R_g, P_g, S_g`` -- groups ``4g .. 4g + 3``, so bank ``g % 4`` is the register's class and no
pass meets a bank conflict -- and five scalars ``ps_g, al_g, be_g, rz_g, rz2_g``.  Queues: ``B`` and
``S`` in, ``P`` and ``X`` out, each in its register's format.

**The execute program** for a job ``(nit, k, n)`` (:func:`cg_programs`), the order of ``cg.vec_step``::

    wf.setvl k
    for g:  vmv.v.i X_g, 0                                        # X = 0
    for g:  wf.vlse.c R_g, qB, g ; vmv.v.v.c P_g, R_g ; wf.vdotr rz_g, R_g, R_g
    wf.qpop qB ; for g: wf.vsse qP, P_g, g ; wf.qpush qP          # P0 out
    for it in 1..nit, for g:
        wf.vlse.c S_g, qS, g ; wf.vdotr ps_g, P_g, S_g             # pass 1: ps = q(Re(P^H S))
        wf.sdivz al_g, rz_g, ps_g                                  # alpha, 0 if ps == 0
        wf.vmacc.vs.c X_g, al_g, P_g ; wf.vnmsac.vs.c R_g, al_g, S_g ; wf.vdotr rz2_g, R_g, R_g
        wf.sdivz be_g, rz2_g, rz_g                                 # beta, 0 if rz == 0
        wf.vmadd.vs.c P_g, be_g, R_g ; wf.vsse qP, P_g, g          # pass 3: P = q(P beta + R), out
    ... wf.qpop qS ; wf.qpush qP

The names ``rz_g`` and ``rz2_g`` swap every iteration (renaming in the program, not in hardware), so
no ``wf.smv`` is needed.  In the last iteration ``R``, ``rz'``, ``beta`` and ``P`` feed nothing, so
by default (``skip_dead``) its second pass is ``vmacc.vs.c X_g, al_g, P_g ; wf.vsse qX, X_g, g`` and
there is no third: the same ``X``, fewer passes.

**Load and store programs**: ``wf.ld qB``, then ``nit`` times ``wf.ld qS``; ``nit`` times ``wf.st``
from ``qP`` (``P0 .. P(nit-1)``), then one from ``qX``.  The harness (:func:`run_cg`) plays the
systolic array: each ``S`` it feeds is ``cg.mm_step(A, P)`` on the ``P`` the unit last emitted.
"""

from __future__ import annotations

import numpy as np

from waveflow.linalg import cg
from waveflow.vecunit import isa as I
from waveflow.vecunit.asm import Programs
from waveflow.vecunit.config import VecUnitConfig
from waveflow.vecunit.model import JobResult, run_job

Q_B, Q_S = 0, 1  # input queues
Q_P, Q_X = 0, 1  # output queues
PORT = 0


def regs(g: int) -> tuple[int, int, int, int]:
    """``X_g, R_g, P_g, S_g``: groups ``4g .. 4g + 3``, one a bank."""
    return 4 * g, 4 * g + 1, 4 * g + 2, 4 * g + 3


def scalars(g: int) -> tuple[int, int, int, int, int]:
    """``ps_g, al_g, be_g, rz_g, rz2_g``."""
    return 5 * g, 5 * g + 1, 5 * g + 2, 5 * g + 3, 5 * g + 4


def cg_config(f: cg.CgFormats, *, Kmax: int = 8, Nmax: int = 32, L: int = 4,
              word_bits: int = 64) -> VecUnitConfig:
    """A unit that runs CG on ``k x n`` jobs up to ``Kmax x Nmax``, with ``f``'s formats."""
    ng = -(-Nmax // L)
    return VecUnitConfig(
        group_formats=(f.X, f.R, f.P, f.S) * ng,
        scalar_formats=(f.ps, f.alpha, f.beta, f.rz, f.rz) * ng,
        in_queue_formats=(f.B, f.S), out_queue_formats=(f.P, f.X),
        L=L, VLMAX=Kmax, Kmax=Kmax, Nmax=Nmax, NB=4, g_div=f.g_div, word_bits=word_bits)


def cg_programs(cfg: VecUnitConfig, nit: int, k: int, n: int, *, skip_dead: bool = True,
                tag0: int = 0) -> Programs:
    """The three programs of one CG job: ``nit`` iterations on a ``k x n`` right-hand side."""
    G = cfg.groups_per_row(n)
    ex = [I.setvl(k)]
    ex += [I.vmv_v_i(regs(g)[0], 0) for g in range(G)]
    for g in range(G):
        _X, R, P, _S = regs(g)
        rz = scalars(g)[3]
        ex += [I.vlse(R, Q_B, g, G).c, I.vmv_v_v(P, R).c, I.vdotr(rz, R, R)]
    ex += [I.qpop(Q_B)] + [I.vsse(Q_P, regs(g)[2], g, G) for g in range(G)] + [I.qpush(Q_P)]
    swapped = False
    for it in range(1, nit + 1):
        last = it == nit
        for g in range(G):
            X, R, P, S = regs(g)
            ps, al, be, rz, rz2 = scalars(g)
            if swapped:
                rz, rz2 = rz2, rz
            ex += [I.vlse(S, Q_S, g, G).c, I.vdotr(ps, P, S), I.sdivz(al, rz, ps)]
            if last and skip_dead:
                ex += [I.vmacc_vs(X, al, P).c, I.vsse(Q_X, X, g, G)]
                continue
            ex += [I.vmacc_vs(X, al, P).c, I.vnmsac_vs(R, al, S).c, I.vdotr(rz2, R, R),
                   I.sdivz(be, rz2, rz)]
            ex += [I.vmadd_vs(P, be, R).c, I.vsse(Q_P, P, g, G)] if not last else [I.vmadd_vs(P, be, R)]
            if last:
                ex += [I.vsse(Q_X, X, g, G)]
        ex += [I.qpop(Q_S), I.qpush(Q_X if last else Q_P)]
        swapped = not swapped
    load = [I.ld(Q_B, PORT, k, n)] + [I.ld(Q_S, PORT, k, n) for _ in range(nit)]
    store = [I.st(PORT, Q_P, tag0 + it, k, n) for it in range(nit)]
    store.append(I.st(PORT, Q_X, tag0 + nit, k, n))
    return Programs(load=load, execute=ex, store=store)


def run_cg(cfg: VecUnitConfig, f: cg.CgFormats, ar, ai, br, bi, nit: int, *, tx_id: int = 0,
           mode: str = "pass", skip_dead: bool = True) -> JobResult:
    """One CG solve through the interpreter: ``B`` in, and each ``S = cg.mm_step(A, P)`` computed
    from the ``P`` the unit has just emitted (the systolic array's part).  ``result.outputs[0]``
    holds ``P0 .. P(nit-1)`` then ``X``."""
    k, n = np.shape(br)
    progs = cg_programs(cfg, nit, k, n, skip_dead=skip_dead)

    def systolic(port: int, result: JobResult):
        _resp, pr, pi = result.outputs[PORT][-1]
        return cg.mm_step(ar, ai, pr, pi, f)

    return run_job(cfg, *progs, {PORT: [(br, bi)]}, tx_id=tx_id, mode=mode, on_starve=systolic)
