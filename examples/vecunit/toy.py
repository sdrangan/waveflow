"""Three toy programs for the vector unit, each introducing one idea (``docs/guide/dsp/vecunit/``).

One small unit -- four lanes, ``VLMAX`` 16, one lane group a row -- runs three kernels on ``k x n``
matrices of complex fixed-point values (``n <= L``: each column is a problem of its own, on a lane):

=========  =====================================  ==============================================
kernel     computes                               introduces
=========  =====================================  ==============================================
``double`` ``Y = X + X``                          a job: three programs, one chained pass, queues
``cmul``   ``Z = conj(X) * Y``, elementwise       chaining up to ``S`` slots, the conjugate form,
                                                  strip-mining a long matrix in chunks of ``VLMAX``;
                                                  with ``rs``, a result shift (``wf.setrs``)
``gain``   ``r = y - a x``, ``a = Re(x^H y) /     reductions, the scalar file, the divide, a
           |x|^2`` per column                     scalar times a vector (``.vs``)
``poly``   ``y = a x^2 + b x + c``, constants     scalar immediates (``wf.sli``), a scalar with a
                                                  vector (``wf.vmul.vs``, ``wf.vadd.vs``)
=========  =====================================  ==============================================

``cmul`` is the matched filter's pass (``examples/vecunit_mf``) without the broadcast; ``gain`` is the
shape of a CG step (``examples/vecunit_cg``): reductions to scalars, a divide, an update.  Each kernel
has a NumPy golden on stored integers (:mod:`waveflow.utils.fixputils`), independent of the
interpreter.  ``poly``'s two passes depend on each other: on a unit with ``fuse_raw_vl`` they fuse
into one run (:func:`toy_config`'s ``fuse_raw_vl``).
"""

from __future__ import annotations

import numpy as np

from waveflow.utils import fixputils as fx
from waveflow.utils.fixputils import Format, OMode, QMode
from waveflow.vecunit import isa as I
from waveflow.vecunit.asm import Programs
from waveflow.vecunit.config import VecUnitConfig
from waveflow.vecunit.model import JobResult, run_job

#: Every vector group and queue: 16 bits, 3 integer bits (values in [-4, 4)), rounding, saturating.
FV = Format(16, 3, True, QMode.AP_RND, OMode.AP_SAT)
#: The two sums of ``gain``: 32 bits, 12 integer bits.
FS = Format(32, 12, True, QMode.AP_RND, OMode.AP_SAT)
#: ``gain``'s ``a``: 16 bits, 4 integer bits.
FA = Format(16, 4, True, QMode.AP_RND, OMode.AP_SAT)

#: Vector groups (one a bank), scalars, queues, the port.
V0, V1, V2 = 0, 1, 2
S_XY, S_XX, S_A = 0, 1, 2
Q_X, Q_Y = 0, 1  # input queues
Q_Z = 0  # output queue
PORT = 0


def toy_config(*, word_bits: int = 64, VLMAX: int = 16, fuse_raw_vl: int = 0) -> VecUnitConfig:
    """The toy unit: four groups and three scalars, two input queues and one output queue, four lanes,
    ``VLMAX`` 16, blocks of up to ``VLMAX`` rows and 4 columns (one lane group a row), three slots,
    the result shift up to 15.  ``fuse_raw_vl``: dependent passes fuse at that ``VL`` and above."""
    return VecUnitConfig(group_formats=(FV,) * 4, scalar_formats=(FS, FS, FA),
                         in_queue_formats=(FV, FV), out_queue_formats=(FV,),
                         L=4, VLMAX=VLMAX, Kmax=VLMAX, Nmax=4, S=3, NB=4, word_bits=word_bits,
                         fuse_raw_vl=fuse_raw_vl)


# --- the programs ------------------------------------------------------------------------------------


def double_programs(k: int, n: int, *, chain: bool = True) -> Programs:
    """``Y = X + X`` on one ``k x n`` matrix: one pass of three chained instructions, or with
    ``chain=False`` three passes of one (the same bits; what a unit of one slot runs)."""
    c = (lambda x: x.c) if chain else (lambda x: x)
    return Programs(
        load=[I.ld(Q_X, PORT, k, n)],
        execute=[I.setvl(k),
                 c(I.vlse(V0, Q_X, 0, 1)),      # v0 = X       (read the block)
                 c(I.vadd_vv(V1, V0, V0)),      # v1 = v0 + v0
                 I.vsse(Q_Z, V1, 0, 1),         # Y = v1       (write the block)
                 I.qpop(Q_X), I.qpush(Q_Z)],
        store=[I.st(PORT, Q_Z, 0, k, n)])


def chunks(cfg: VecUnitConfig, nrows: int) -> list[int]:
    """The rows of each chunk of a ``nrows``-row matrix: ``VLMAX`` each, the last one shorter."""
    return [min(cfg.VLMAX, nrows - r0) for r0 in range(0, nrows, cfg.VLMAX)]


def cmul_programs(cfg: VecUnitConfig, nrows: int, n: int, *, rs: int = 0) -> Programs:
    """``Z = conj(X) * Y`` on ``nrows x n`` matrices, strip-mined: per chunk of ``VLMAX`` rows, ``X``
    read alone, then ``Y`` read, multiplied and written in one chained pass.  ``rs``: every product
    scaled by ``2**-rs`` (``wf.setrs rs`` first)."""
    p = Programs()
    if rs:
        p.execute.append(I.setrs(rs))
    for c, k in enumerate(chunks(cfg, nrows)):
        p.load += [I.ld(Q_X, PORT, k, n), I.ld(Q_Y, PORT, k, n)]
        p.execute += [I.setvl(k),
                      I.vlse(V0, Q_X, 0, 1),                          # v0 = X
                      I.vlse(V1, Q_Y, 0, 1).c,                        # v1 = Y
                      I.vmul_vv(V2, V0, V1, conj=True).c,             # v2 = conj(v0) * v1
                      I.vsse(Q_Z, V2, 0, 1),                          # Z = v2
                      I.qpop(Q_X), I.qpop(Q_Y), I.qpush(Q_Z)]
        p.store.append(I.st(PORT, Q_Z, c, k, n))
    return p


def gain_programs(k: int, n: int) -> Programs:
    """Per column, ``a = Re(x^H y) / |x|^2`` and ``r = y - a x``: two reductions, a divide, an update."""
    return Programs(
        load=[I.ld(Q_X, PORT, k, n), I.ld(Q_Y, PORT, k, n)],
        execute=[I.setvl(k),
                 I.vlse(V0, Q_X, 0, 1).c, I.vdotr(S_XX, V0, V0),     # v0 = x;  |x|^2
                 I.vlse(V1, Q_Y, 0, 1).c, I.vdotr(S_XY, V0, V1),     # v1 = y;  Re(x^H y)
                 I.qpop(Q_X), I.qpop(Q_Y),
                 I.sdivz(S_A, S_XY, S_XX),                           # a = Re(x^H y) / |x|^2
                 I.vnmsac_vs(V1, S_A, V0).c,                         # v1 = v1 - a v0
                 I.vsse(Q_Z, V1, 0, 1),                              # r = v1
                 I.qpush(Q_Z)],
        store=[I.st(PORT, Q_Z, 0, k, n)])


def stored(value: float, fmt: Format) -> int:
    """A real constant as ``fmt``'s stored integer (rounded to its fraction bits)."""
    return int(round(value * (1 << fmt.frac_bits)))


def poly_programs(cfg: VecUnitConfig, nrows: int, n: int, a: float, b: float,
                  c: float) -> Programs:
    """``y = a x^2 + b x + c`` (Horner: ``(a x + b) x + c``) on an ``nrows x n`` matrix, strip-mined,
    the constants scalar immediates set once: per chunk, pass 1 ``v1 = a x``, ``v2 = v1 + b``; pass 2
    ``v3 = v2 x``, ``v1 = v3 + c``, written out.  Pass 2 reads what pass 1 wrote: two runs, or one on a
    unit with ``fuse_raw_vl <= VL``."""
    fs = cfg.scalar_formats
    p = Programs(execute=[I.sli(S_XY, stored(a, fs[S_XY])), I.sli(S_XX, stored(b, fs[S_XX])),
                          I.sli(S_A, stored(c, fs[S_A]))])
    for ch, k in enumerate(chunks(cfg, nrows)):
        p.load.append(I.ld(Q_X, PORT, k, n))
        p.execute += [I.setvl(k),
                      I.vlse(V0, Q_X, 0, 1).c,          # pass 1: v0 = x
                      I.vmul_vs(V1, S_XY, V0).c,        #         v1 = a x
                      I.vadd_vs(V2, S_XX, V1),          #         v2 = a x + b
                      I.vmul_vv(3, V2, V0).c,           # pass 2: v3 = v2 x
                      I.vadd_vs(V1, S_A, 3).c,          #         v1 = v3 + c
                      I.vsse(Q_Z, V1, 0, 1),            #         y = v1
                      I.qpop(Q_X), I.qpush(Q_Z)]
        p.store.append(I.st(PORT, Q_Z, ch, k, n))
    return p


# --- running them on the interpreter -----------------------------------------------------------------


def run(cfg: VecUnitConfig, progs: Programs, mats: list, *, tx_id: int = 0,
        mode: str = "pass") -> tuple[np.ndarray, np.ndarray, JobResult]:
    """One job through the interpreter: *mats* are the ``(re, im)`` matrices the load program takes,
    in its order; the result is the output port's matrices stacked by row, and the job's result."""
    r = run_job(cfg, *progs, {PORT: mats}, tx_id=tx_id, mode=mode)
    outs = r.outputs[PORT]
    return np.concatenate([o[1] for o in outs]), np.concatenate([o[2] for o in outs]), r


def cmul_inputs(cfg: VecUnitConfig, xr, xi, yr, yi) -> list:
    """``cmul``'s bursts as its load program takes them: per chunk ``X`` then ``Y``."""
    out, r0 = [], 0
    for k in chunks(cfg, len(xr)):
        out += [(xr[r0:r0 + k], xi[r0:r0 + k]), (yr[r0:r0 + k], yi[r0:r0 + k])]
        r0 += k
    return out


def random_matrix(k: int, n: int, seed: int, scale: float = 1.0):
    """A ``k x n`` matrix of stored integers in :data:`FV`, uniform in ``[-scale, scale)``."""
    rng = np.random.default_rng(seed)
    hi = int(scale * (1 << FV.frac_bits))
    return rng.integers(-hi, hi, (k, n)), rng.integers(-hi, hi, (k, n))


# --- goldens: NumPy on stored integers ---------------------------------------------------------------


def double_golden(xr, xi):
    """``q(X + X)``."""
    re, f = fx.add(xr, FV, xr, FV)
    im, _ = fx.add(xi, FV, xi, FV)
    return fx.quantize(re, f, FV), fx.quantize(im, f, FV)


def _scaled(f: Format, rs: int) -> Format:
    """The same stored integers, ``rs`` more fraction bits: a value times ``2**-rs``, exact."""
    return Format(f.W + rs, f.int_bits, True) if rs else f


def cmul_golden(xr, xi, yr, yi, rs: int = 0):
    """``q(conj(X) * Y * 2**-rs)``, exact before the one rounding."""
    p_rr, fp = fx.mult(xr, FV, yr, FV)
    p_ii, _ = fx.mult(xi, FV, yi, FV)
    p_ri, _ = fx.mult(xr, FV, yi, FV)
    p_ir, _ = fx.mult(xi, FV, yr, FV)
    re, f_re = fx.add(p_rr, fp, p_ii, fp)  # Re(conj(x) y) = xr yr + xi yi
    im, f_im = fx.sub(p_ri, fp, p_ir, fp)  # Im(conj(x) y) = xr yi - xi yr
    return fx.quantize(re, _scaled(f_re, rs), FV), fx.quantize(im, _scaled(f_im, rs), FV)


def poly_golden(cfg: VecUnitConfig, xr, xi, a: float, b: float, c: float):
    """``y = q(q(q(q(a x) + b) x) + c)``: each write rounded to ``FV``, the constants to their
    scalars' formats, a scalar added to the real part only."""
    fs = cfg.scalar_formats
    sa, sb, sc = (stored(v, fs[j]) for v, j in ((a, S_XY), (b, S_XX), (c, S_A)))

    def smul(s, f, vr, vi):  # q(s v), s real
        return (fx.quantize(*fx.mult(s, f, vr, FV), FV), fx.quantize(*fx.mult(s, f, vi, FV), FV))

    def sadd(s, f, vr, vi):  # q(v + s) on the real part
        return fx.quantize(*fx.add(vr, FV, s, f), FV), fx.quantize(vi, FV, FV)

    t1 = smul(sa, fs[S_XY], xr, xi)
    t2 = sadd(sb, fs[S_XX], *t1)
    p_rr, fp = fx.mult(t2[0], FV, xr, FV)
    p_ii, _ = fx.mult(t2[1], FV, xi, FV)
    p_ri, _ = fx.mult(t2[0], FV, xi, FV)
    p_ir, _ = fx.mult(t2[1], FV, xr, FV)
    t3 = fx.quantize(*fx.sub(p_rr, fp, p_ii, fp), FV), fx.quantize(*fx.add(p_ri, fp, p_ir, fp), FV)
    return sadd(sc, fs[S_A], *t3)


def gain_golden(xr, xi, yr, yi):
    """``r = q(y - a x)`` per column, ``a = q(q(Re(x^H y)) / q(|x|^2))``: each sum exact, then one
    rounding to its scalar; the quotient at the dividend's fraction bits, then rounded to ``a``."""
    def dotr(ar, ai, br, bi):
        p_r, fp = fx.mult(ar, FV, br, FV)
        p_i, _ = fx.mult(ai, FV, bi, FV)
        t, ft = fx.add(p_r, fp, p_i, fp)
        s, fs = fx.fixed_sum(t, ft, axis=0)
        return fx.quantize(s, fs, FS)

    xy, xx = dotr(xr, xi, yr, yi), dotr(xr, xi, xr, xi)
    q, fq = fx.div(xy, FS, xx, FS)  # 0 where |x|^2 == 0
    a = fx.quantize(q, fq, FA)
    pr, fp = fx.mult(a, FA, xr, FV)
    pi, _ = fx.mult(a, FA, xi, FV)
    rr, fr = fx.sub(yr, FV, pr, fp)
    ri, _ = fx.sub(yi, FV, pi, fp)
    return fx.quantize(rr, fr, FV), fx.quantize(ri, fr, FV)
