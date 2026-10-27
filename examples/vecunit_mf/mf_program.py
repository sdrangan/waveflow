"""A multi-antenna matched filter as a program for the vector unit (``plans/vecunit_new.md``).

``Y[i, j] = conj(X[i]) * R[i, j]``, ``i`` over the samples of a frame (subcarriers), ``j < nrx`` over
the receive antennas:

========================  ===========================================================================
matched filter            unit
========================  ===========================================================================
antenna ``j``             a group member, one lane an antenna; ``nrx > L`` takes ``ceil(nrx / L)``
                          column groups, and ``L`` need not divide ``nrx``
sample ``i``              an element; a chunk of ``VL <= VLMAX`` samples at a time, strip-mined
``R`` (samples x nrx)     an input block per chunk, ``k`` = the chunk, ``n`` = ``nrx``
``X`` (samples)           a single-column input block (``k x 1``), read into member 0
``Y``                     an output block per chunk, the layout of ``R``
========================  ===========================================================================

Per chunk the execute program reads ``X`` once into ``v0`` (member 0 holds it; the other lanes
are the block's zero padding), then runs one chained pass per column group ``g``: read ``R``'s
group into ``v1``, ``v2 = conj(v0[member 0]) * v1`` (the conjugate and broadcast forms), and write
``v2`` straight to the output block::

    wf.setvl k
    wf.vlse v0, qi0, 0, 1 ; wf.qpop qi0
    for g:  wf.vlse.c v1, qi1, g, G ; wf.vmulc.vb.c v2, v0, v1 ; wf.vsse qo0, v2, g, G
    wf.qpop qi1 ; wf.qpush qo0

``5 + 3·ceil(nrx / L)`` instructions a chunk; each pass moves ``k`` elements.  The load program
alternates ``wf.ld`` of ``X`` and ``R`` per chunk; the store program is one ``wf.st`` per chunk
(tagged with the chunk's index).
"""

from __future__ import annotations

import numpy as np

from waveflow.utils.fixputils import Format
from waveflow.vecunit import isa as I
from waveflow.vecunit.asm import Programs
from waveflow.vecunit.config import VecUnitConfig
from waveflow.vecunit.model import JobResult, run_job

#: Registers and queues.
V_X, V_R, V_Y = 0, 1, 2
Q_X, Q_R = 0, 1  # input queues
Q_Y = 0  # output queue
PORT = 0


def mf_config(fx: Format, fr: Format, fy: Format, *, L: int = 4, VLMAX: int = 64,
              nrx_max: int = 8, word_bits: int = 64) -> VecUnitConfig:
    """A unit for the matched filter: four groups (``X``, ``R``, ``Y`` and a spare, one a bank),
    queues ``X``, ``R`` in and ``Y`` out; blocks of ``VLMAX`` rows and ``nrx_max`` columns."""
    return VecUnitConfig(
        group_formats=(fx, fr, fy, fy), scalar_formats=(fy,),
        in_queue_formats=(fx, fr), out_queue_formats=(fy,),
        L=L, VLMAX=VLMAX, Kmax=VLMAX, Nmax=nrx_max, NB=4, word_bits=word_bits)


def chunks(cfg: VecUnitConfig, nsamp: int) -> list[tuple[int, int]]:
    """``(first sample, length)`` of each chunk: ``VLMAX`` samples, the last one shorter."""
    return [(c0, min(cfg.VLMAX, nsamp - c0)) for c0 in range(0, nsamp, cfg.VLMAX)]


def mf_programs(cfg: VecUnitConfig, nsamp: int, nrx: int) -> Programs:
    """The three programs for one frame of ``nsamp`` samples and ``nrx`` antennas."""
    G = cfg.groups_per_row(nrx)
    p = Programs()
    for c, (_c0, k) in enumerate(chunks(cfg, nsamp)):
        p.load += [I.ld(Q_X, PORT, k, 1), I.ld(Q_R, PORT, k, nrx)]
        p.execute += [I.setvl(k), I.vlse(V_X, Q_X, 0, 1), I.qpop(Q_X)]
        for g in range(G):
            p.execute += [I.vlse(V_R, Q_R, g, G).c,
                          I.vmul_vv(V_Y, V_X, V_R, conj=True, bcast=True).c,
                          I.vsse(Q_Y, V_Y, g, G)]
        p.execute += [I.qpop(Q_R), I.qpush(Q_Y)]
        p.store.append(I.st(PORT, Q_Y, c, k, nrx))
    return p


def instructions_per_chunk(cfg: VecUnitConfig, nrx: int) -> int:
    """Execute instructions a chunk: ``5 + 3·ceil(nrx / L)``."""
    return 5 + 3 * cfg.groups_per_row(nrx)


def mf_inputs(cfg: VecUnitConfig, xr, xi, rr, ri) -> dict[int, list]:
    """The bursts on the input port, as the load program takes them: per chunk ``X`` then ``R``."""
    out = []
    for c0, k in chunks(cfg, len(xr)):
        sl = slice(c0, c0 + k)
        out.append((np.asarray(xr)[sl, None], np.asarray(xi)[sl, None]))
        out.append((np.asarray(rr)[sl], np.asarray(ri)[sl]))
    return {PORT: out}


def run_mf(cfg: VecUnitConfig, xr, xi, rr, ri, *, tx_id: int = 0,
           mode: str = "pass", checks: dict | None = None) -> tuple[np.ndarray, np.ndarray, JobResult]:
    """One frame through the interpreter: ``(Y re, Y im, the job's result)``."""
    nsamp, nrx = np.shape(rr)
    p = mf_programs(cfg, nsamp, nrx)
    r = run_job(cfg, *p, mf_inputs(cfg, xr, xi, rr, ri), tx_id=tx_id, mode=mode,
                checks=checks)
    outs = r.outputs[PORT]
    yr = np.concatenate([o[1] for o in outs]) if outs else np.zeros((0, nrx), np.int64)
    yi = np.concatenate([o[2] for o in outs]) if outs else np.zeros((0, nrx), np.int64)
    return yr, yi, r
