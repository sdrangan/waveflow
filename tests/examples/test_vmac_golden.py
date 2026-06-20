"""VMAC golden tests — bit-level results for every op vs an independent oracle.

VMAC is **complex-only**: every element is an ``(re, im)`` pair, and the numeric format is
structural (on :class:`~examples.vmac.vmac.VmacAccel`), not in the command.  The production
golden (:meth:`VmacAccel.execute`) composes the integer-backed numpy ``ComplexField``
operators.  The **oracle** here is independent: exact rational (``Fraction``) arithmetic over
``(re, im)`` pairs, the accumulator value computed exactly (full precision), then quantized once
with the right-shift → round → saturate spec.  The requantize shift is **derived** — the output
sits at the input fractional scale ``F_out = F_in`` (``SHIFT = F_acc − F_in``), exactly as
:meth:`VmacAccel.output_format` defines.  Two independent implementations must agree bit-for-bit;
a few literal hand-checked outputs anchor the oracle.

The three element-wise ops are ``scalar_mult`` (``alpha[i]·A``), ``inner_prod`` (``A·conj(B)``),
and ``sum`` (``A+B``), each with an optional row reduction ``Y[j] = Σ_i R[i, j]``.  Operands are
supplied as **stored integers** at fractional scale ``F = in_bw - int_bits``; real value =
``stored · 2**-F`` (exact).  Real-valued cases just carry ``im = 0``.
"""

import math
from fractions import Fraction

import numpy as np
import pytest

from examples.vmac.vmac import VmacAccel
from examples.vmac.vmac_cmd import OpCode
from waveflow.utils import complexutils as cx
from waveflow.utils.fixputils import Format


# --- exact (re, im) Fraction complex arithmetic -------------------------------
def _cmul(a, b):
    (ar, ai), (br, bi) = a, b
    return (ar * br - ai * bi, ar * bi + ai * br)


def _q_real(value: Fraction, out_frac: int, W: int, q_rnd: bool, o_sat: bool) -> int:
    """Quantize an exact real ``value`` to ``W`` bits at ``out_frac`` frac bits, signed."""
    scaled = value * (Fraction(2) ** out_frac)
    f = math.floor(scaled + Fraction(1, 2)) if q_rnd else math.floor(scaled)
    if o_sat:
        return max(-(1 << (W - 1)), min((1 << (W - 1)) - 1, f))
    y = f & ((1 << W) - 1)
    return y - (1 << W) if (y >> (W - 1)) & 1 else y


# --- the oracle (independent of the production golden) ------------------------
def oracle(cfg, a, b, alpha):
    """a/b: (n, m) stored-int pairs (re, im arrays); alpha: a scalar (re, im) or per-row (n,)
    array (scalar_mult only).  Returns the expected dst stored ints — (n, m) or (m,) reduced —
    as a pair (re, im) of int arrays."""
    F = cfg["in_bw"] - cfg["int_bits"]
    scale = Fraction(2) ** F
    op = cfg["op"]
    n, m = a[0].shape

    def fr(stored):
        return Fraction(int(stored), 1) / scale

    def at(operand, i, j):
        return (fr(operand[0][i, j]), fr(operand[1][i, j]))

    def alpha_at(i):
        if np.ndim(alpha[0]) == 0:
            return (fr(alpha[0]), fr(alpha[1]))
        return (fr(alpha[0][i]), fr(alpha[1][i]))

    out_frac = F  # F_out = F_in (derived shift = F_acc - F_in)
    cols_re, cols_im = [], []
    for j in range(m):
        terms = []
        for i in range(n):
            av = at(a, i, j)
            if op is OpCode.scalar_mult:
                t = _cmul(alpha_at(i), av)
            elif op is OpCode.inner_prod:
                bv = at(b, i, j)
                t = _cmul(av, (bv[0], -bv[1]))  # A · conj(B)
            else:  # sum
                bv = at(b, i, j)
                t = (av[0] + bv[0], av[1] + bv[1])
            terms.append(t)
        if cfg["reduce"]:
            acc = terms[0]
            for t in terms[1:]:
                acc = (acc[0] + t[0], acc[1] + t[1])
            rows = [acc]
        else:
            rows = terms
        cols_re.append(
            [
                _q_real(r[0], out_frac, cfg["out_bw"], cfg["q_rnd"], cfg["o_sat"])
                for r in rows
            ]
        )
        cols_im.append(
            [
                _q_real(r[1], out_frac, cfg["out_bw"], cfg["q_rnd"], cfg["o_sat"])
                for r in rows
            ]
        )
    re = np.array(cols_re, dtype=np.int64).T  # (rows, m)
    im = np.array(cols_im, dtype=np.int64).T
    if cfg["reduce"]:
        re, im = re[0], im[0]  # (m,)
    return re, im


# --- harness: lay operands into mem + build the matching VmacCmd --------------
def _flat(pair):
    re = np.asarray(pair[0]).ravel()
    im = np.asarray(pair[1]).ravel()
    return cx.make_complex(
        re, im, Format(8, 4, True)
    )  # dtype only; format irrelevant here


def _accel(cfg):
    return VmacAccel(
        mem_dwidth=512,
        mem_awidth=32,
        data_bw=cfg["in_bw"],
        int_bits=cfg["int_bits"],
        acc_bw=cfg.get("acc_bw", 48),
        out_bw=cfg["out_bw"],
        q_rnd=cfg["q_rnd"],
        o_sat=cfg["o_sat"],
    )


def _alpha_field(op, alpha, addr):
    if op is OpCode.scalar_mult and np.ndim(alpha[0]) > 0:  # per-row indirect
        return {"direct": 0, "imm": (0, 0), "addr": int(addr), "stride": 1}
    re = int(alpha[0]) if np.ndim(alpha[0]) == 0 else 0
    im = int(alpha[1]) if np.ndim(alpha[0]) == 0 else 0
    return {"direct": 1, "imm": (re, im), "addr": 0, "stride": 0}


def build_cmd(accel, cfg, a, b, alpha):
    """Build the matching :class:`VmacCmd` (op + reduce + geometry + alpha field).  ``execute`` is
    now the pure golden — it takes dense operand arrays, not a flat ``mem`` — so no memory layout
    is needed; the region addr / row_stride fields are still set to valid values (nothing in
    ``execute`` indexes them)."""
    op = cfg["op"]
    n, m = a[0].shape
    cmd = accel.Cmd()
    cmd.op, cmd.reduce, cmd.n_rows, cmd.n_cols = op, int(cfg["reduce"]), n, m
    for name in ("a", "b", "y"):
        setattr(cmd, name, {"addr": 0, "row_stride": m})
    cmd.alpha = _alpha_field(op, alpha, 0)
    return cmd


def run(cfg, a, b, alpha):
    accel = _accel(cfg)
    cmd = build_cmd(accel, cfg, a, b, alpha)
    op = cfg["op"]
    # dense operand arrays (structured complex) straight to the pure golden — no flat mem
    a_arr = _flat(a)
    b_arr = _flat(b) if op in (OpCode.inner_prod, OpCode.sum) else None
    alpha_arr = (
        _flat(alpha) if op is OpCode.scalar_mult and np.ndim(alpha[0]) > 0 else None
    )
    dst = accel.execute(cmd, a_arr, b_arr, alpha_arr)
    exp_re, exp_im = oracle(cfg, a, b, alpha)
    got_re, got_im = np.asarray(dst.val["re"]), np.asarray(dst.val["im"])
    return (got_re, got_im), (exp_re, exp_im)


# --- operand helpers ----------------------------------------------------------
def _pair(re, im=None):
    re = np.asarray(re, dtype=np.int64)
    im = np.zeros_like(re) if im is None else np.asarray(im, dtype=np.int64)
    return (re, im)


def _cfg(**kw):
    # VMAC is complex-only; the command carries only op + reduce + geometry. The numeric
    # format (in_bw / int_bits / out_bw / acc_bw / q_rnd / o_sat) is structural (passed to
    # the accelerator via _accel); the requantize shift is derived (F_acc - F_in).
    base = dict(
        op=OpCode.sum,
        reduce=0,
        in_bw=8,
        int_bits=4,
        out_bw=8,
        acc_bw=48,
        q_rnd=0,
        o_sat=0,
    )
    base.update(kw)
    return base


# --- literal hand-checked anchors --------------------------------------------
def test_scalar_mult_literal():
    # R = alpha · A, alpha = 2.0 (code 32), A = [1.5, 2.0] = [24, 32] -> [3.0, 4.0] = [48, 64].
    cfg = _cfg(op=OpCode.scalar_mult)
    (gr, _), (er, _) = run(cfg, _pair([[24, 32]]), _pair([[0, 0]]), _pair(32, 0))
    np.testing.assert_array_equal(gr, [[48, 64]])
    np.testing.assert_array_equal(gr, er)


def test_inner_prod_literal():
    # R = A · conj(B): A = 2.0 = 32, B = 1.5 = 24 -> 3.0 = 48 (im 0).
    cfg = _cfg(op=OpCode.inner_prod)
    a, b = _pair([[32]]), _pair([[24]])
    (gr, _), (er, _) = run(cfg, a, b, _pair(16, 0))
    np.testing.assert_array_equal(gr, [[48]])
    np.testing.assert_array_equal(gr, er)


def test_sum_literal():
    # R = A + B: same scale, codes add. A = [1.0, 2.0] = [16, 32], B = [0.5, 0.5] = [8, 8].
    cfg = _cfg(op=OpCode.sum)
    (gr, _), (er, _) = run(cfg, _pair([[16, 32]]), _pair([[8, 8]]), _pair(0, 0))
    np.testing.assert_array_equal(gr, [[24, 40]])
    np.testing.assert_array_equal(gr, er)


def test_sum_reduce_literal():
    # R = A + B, reduce over rows. col A = [1.0, 2.0, 0.5] = [16, 32, 8], B = 0 -> sum 3.5 = 56.
    cfg = _cfg(op=OpCode.sum, reduce=1)
    a = _pair([[16], [32], [8]])
    (gr, _), (er, _) = run(cfg, a, _pair(np.zeros((3, 1))), _pair(0, 0))
    np.testing.assert_array_equal(gr, [56])
    np.testing.assert_array_equal(gr, er)


def test_scalar_mult_per_row_indirect_literal():
    # R[i,j] = alpha[i] · A[i,j], alpha = [1.0, 2.0] = [16, 32] (per row).
    cfg = _cfg(op=OpCode.scalar_mult)
    a = _pair([[16, 32], [16, 16]])  # rows 1.0,2.0 / 1.0,1.0
    (gr, _), (er, _) = run(cfg, a, _pair(np.zeros((2, 2))), _pair([16, 32]))
    np.testing.assert_array_equal(gr, [[16, 32], [32, 32]])  # row0·1, row1·2
    np.testing.assert_array_equal(gr, er)


# --- op × reduce × alpha-mode sweep vs the oracle (complex-only) ---------------
@pytest.mark.parametrize("q_rnd,o_sat", [(0, 0), (1, 0), (0, 1), (1, 1)])
def test_ops_vs_oracle(q_rnd, o_sat):
    rng = np.random.default_rng(2)
    n, m = 4, 3

    def cop():
        return _pair(rng.integers(-60, 61, (n, m)), rng.integers(-60, 61, (n, m)))

    a, b = cop(), cop()
    alpha_s = _pair(20, -8)
    alpha_pr = _pair(rng.integers(-30, 31, n), rng.integers(-30, 31, n))
    for reduce in (0, 1):
        for op in (OpCode.scalar_mult, OpCode.inner_prod, OpCode.sum):
            alphas = [alpha_s, alpha_pr] if op is OpCode.scalar_mult else [alpha_s]
            for alpha in alphas:
                cfg = _cfg(op=op, reduce=reduce, q_rnd=q_rnd, o_sat=o_sat)
                got, exp = run(cfg, a, b, alpha)
                np.testing.assert_array_equal(got[0], exp[0], err_msg=f"{cfg} re")
                np.testing.assert_array_equal(got[1], exp[1], err_msg=f"{cfg} im")


# --- saturation + rounding are actually exercised -----------------------------
def test_saturation_triggered():
    # inner_prod: 7.0 * 7.0 = 49.0, way over the +-8 OUT_BW=8 range -> saturate to max.
    cfg = _cfg(op=OpCode.inner_prod, o_sat=1)
    a, b = _pair([[112]]), _pair([[112]])
    (gr, _), (er, _) = run(cfg, a, b, _pair(16, 0))
    assert gr[0, 0] == 127
    np.testing.assert_array_equal(gr, er)


def test_rounding_triggered():
    # a value landing between LSBs differs under TRN vs RND (derived shift = F_in = 4 here).
    rng = np.random.default_rng(9)
    a = _pair(rng.integers(-120, 121, (3, 2)))
    b = _pair(rng.integers(-120, 121, (3, 2)))
    trn = run(_cfg(op=OpCode.inner_prod, q_rnd=0), a, b, _pair(16, 0))
    rnd = run(_cfg(op=OpCode.inner_prod, q_rnd=1), a, b, _pair(16, 0))
    np.testing.assert_array_equal(trn[0][0], trn[1][0])
    np.testing.assert_array_equal(rnd[0][0], rnd[1][0])
    assert not np.array_equal(trn[0][0], rnd[0][0])  # the two modes genuinely differ


# --- complex-op coverage ------------------------------------------------------
def test_conj_inner_product_reduce():
    # ps[j] = Σ_i S[i,j]·conj(P[i,j]) (the column inner product).
    rng = np.random.default_rng(13)
    n, m = 6, 2
    S = _pair(rng.integers(-50, 51, (n, m)), rng.integers(-50, 51, (n, m)))
    P = _pair(rng.integers(-50, 51, (n, m)), rng.integers(-50, 51, (n, m)))
    got, exp = run(_cfg(op=OpCode.inner_prod, reduce=1), S, P, _pair(16, 0))
    np.testing.assert_array_equal(got[0], exp[0])
    np.testing.assert_array_equal(got[1], exp[1])


def test_rnorm_sum_abs_sq_is_real():
    # rnorm[j] = Σ_i |R[i,j]|² (R·conj(R), reduced) -> REAL (im == 0), non-negative.
    rng = np.random.default_rng(14)
    n, m = 6, 2
    R = _pair(rng.integers(-50, 51, (n, m)), rng.integers(-50, 51, (n, m)))
    cfg = _cfg(op=OpCode.inner_prod, reduce=1, out_bw=12, o_sat=1)
    got, exp = run(cfg, R, R, _pair(16, 0))
    np.testing.assert_array_equal(got[0], exp[0])
    np.testing.assert_array_equal(got[1], np.zeros_like(got[1]))  # |R|² is real
    assert np.all(got[0] >= 0)
