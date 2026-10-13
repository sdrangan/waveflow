"""Step 7.2: the bit-exact model of the matrix multiply (``waveflow.linalg.matmul``).

Two independent checks: the golden vectors frozen from the example's ``mm_step`` at ``6a2cdca``
(``data/mm_golden.npz``, written by ``examples/mimo_cg/tools/freeze_mm_golden.py``), and a
reference that multiplies Python integers one product at a time and rounds with
``fixputils.quantize``.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from waveflow.linalg import matmul as mm
from waveflow.utils import complexutils as cx
from waveflow.utils import fixputils as fx
from waveflow.utils.fixputils import Format, OMode, QMode

DATA = Path(__file__).parent / "data" / "mm_golden.npz"
FORMS = (3, 4)


def reg(W: int, I: int) -> Format:
    return Format(W, I, True, QMode.AP_RND, OMode.AP_SAT)


def _fmt(spec) -> Format:
    W, I, signed, q, o = spec
    return Format(W, I, bool(signed), QMode(q), OMode(o))


@pytest.fixture(scope="module")
def golden():
    with np.load(DATA, allow_pickle=False) as z:
        arrays = {k: z[k].astype(np.int64) for k in z.files if k != "meta"}
        meta = json.loads(str(z["meta"]))
    return meta, arrays


def _case(meta, arrays, case):
    t = meta["triples"][case["triple"]]
    a, b, c = (arrays[f"{case['key']}_{x}"] for x in "abc")
    return a, b, c, _fmt(t["a"]), _fmt(t["b"]), _fmt(t["c"])


# --- golden vectors ------------------------------------------------------------------------------


def test_golden_provenance_and_coverage(golden):
    meta, _ = golden
    assert meta["source_commit"].startswith("6a2cdca")
    names = {f"W{W}g{g}" for W in (8, 10, 12, 14, 16) for g in (0, 4, 8)} | {"stress"}
    assert set(meta["sets"]) == names
    want = {
        (t, K, kind)
        for t in range(len(meta["triples"]))
        for K in (4, 8, 16)
        for kind in ("small", "mid", "full", "extreme")
    }
    assert {(c["triple"], c["K"], c["kind"]) for c in meta["cases"]} == want
    rails = {
        kind: sum(c["n_rail"] for c in meta["cases"] if c["kind"] == kind)
        for kind in ("small", "mid", "full", "extreme")
    }
    assert rails["small"] == 0
    assert rails["mid"] > 0 and rails["full"] > 0 and rails["extreme"] > 0


@pytest.mark.parametrize("form", FORMS)
def test_model_equals_golden(golden, form):
    meta, arrays = golden
    for case in meta["cases"]:
        a, b, c, fa, fb, fc = _case(meta, arrays, case)
        re, im = mm.matmul(a[0], a[1], fa, b[0], b[1], fb, fc, form=form)
        assert np.array_equal(re, c[0]) and np.array_equal(im, c[1]), case["key"]


def test_model_equals_golden_batched(golden):
    """The four kinds of one triple and K as one batch over a leading dimension."""
    meta, arrays = golden
    groups: dict[tuple[int, int], list] = {}
    for case in meta["cases"]:
        groups.setdefault((case["triple"], case["K"]), []).append(case)
    for cases in groups.values():
        parts = [_case(meta, arrays, c) for c in cases]
        a, b, c = (np.stack([p[i] for p in parts], axis=1) for i in range(3))
        fa, fb, fc = parts[0][3:]
        re, im = mm.matmul(a[0], a[1], fa, b[0], b[1], fb, fc)
        assert np.array_equal(re, c[0]) and np.array_equal(im, c[1])


# --- the independent reference -------------------------------------------------------------------


def reference(ar, ai, fa, br, bi, fb, fc, adjoint=False):
    """Python integers, one product at a time, then ``fixputils.quantize``."""
    ar, ai, br, bi = (np.asarray(x, np.int64) for x in (ar, ai, br, bi))
    lead = np.broadcast_shapes(ar.shape[:-2], br.shape[:-2])
    ar, ai = (np.broadcast_to(x, lead + x.shape[-2:]) for x in (ar, ai))
    br, bi = (np.broadcast_to(x, lead + x.shape[-2:]) for x in (br, bi))
    M = ar.shape[-1] if adjoint else ar.shape[-2]
    K, N = br.shape[-2:]
    re = np.zeros(lead + (M, N), np.int64)
    im = np.zeros(lead + (M, N), np.int64)
    for idx in np.ndindex(*lead):
        for i in range(M):
            for j in range(N):
                sr = si = 0
                for k in range(K):
                    if adjoint:
                        xr, xi = int(ar[idx + (k, i)]), -int(ai[idx + (k, i)])
                    else:
                        xr, xi = int(ar[idx + (i, k)]), int(ai[idx + (i, k)])
                    yr, yi = int(br[idx + (k, j)]), int(bi[idx + (k, j)])
                    sr += xr * yr - xi * yi
                    si += xr * yi + xi * yr
                re[idx + (i, j)], im[idx + (i, j)] = sr, si
    F = fa.frac_bits + fb.frac_bits
    src = Format(64, 64 - F, True)
    assert fc.frac_bits <= F  # the reference only rounds down in precision
    return fx.quantize(re, src, fc), fx.quantize(im, src, fc)


def _draw(rng, fmt: Format, shape, scale: float = 1.0):
    lo, hi = -(1 << (fmt.W - 1)), (1 << (fmt.W - 1)) - 1
    v = np.rint(rng.uniform(-1.0, 1.0, size=shape) * scale * 2.0 ** (fmt.W - 1))
    return np.clip(v, lo, hi).astype(np.int64)


#: (A's leading dims, B's leading dims, M, K, N): non-square, K = 1, N = 1, broadcasting.
SHAPES = [
    ((), (), 3, 5, 7),
    ((2,), (2,), 8, 4, 2),
    ((2, 3), (2, 3), 1, 16, 3),
    ((), (), 5, 1, 4),
    ((3,), (3,), 6, 9, 1),
    ((2, 1), (3,), 4, 6, 5),
]
#: (a, b, c): unequal operand widths; c rounding and saturating, or truncating and wrapping.
FORMAT_SETS = [
    (reg(12, 3), reg(12, 4), reg(12, 5)),
    (reg(10, 2), reg(14, 5), reg(12, 4)),
    (reg(16, 3), reg(8, 1), Format(13, 6, True, QMode.AP_TRN, OMode.AP_WRAP)),
]


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("adjoint", [False, True])
@pytest.mark.parametrize("fset", range(len(FORMAT_SETS)))
@pytest.mark.parametrize("shape", range(len(SHAPES)))
def test_model_equals_reference(shape, fset, adjoint, form):
    lead_a, lead_b, M, K, N = SHAPES[shape]
    fa, fb, fc = FORMAT_SETS[fset]
    rng = np.random.default_rng([7, 2, shape, fset, int(adjoint)])
    a_shape = lead_a + ((K, M) if adjoint else (M, K))
    for scale in (1.0, 0.1):  # most results saturate (or wrap), then few
        ar, ai = _draw(rng, fa, a_shape, scale), _draw(rng, fa, a_shape, scale)
        br, bi = _draw(rng, fb, lead_b + (K, N), scale), _draw(
            rng, fb, lead_b + (K, N), scale
        )
        got = mm.matmul(ar, ai, fa, br, bi, fb, fc, adjoint=adjoint, form=form)
        want = reference(ar, ai, fa, br, bi, fb, fc, adjoint)
        assert np.array_equal(got[0], want[0]) and np.array_equal(got[1], want[1])


@pytest.mark.parametrize("form", FORMS)
@pytest.mark.parametrize("adjoint", [False, True])
def test_most_negative_imaginary_parts(adjoint, form):
    """Every imaginary part is -2^(W-1), whose conjugate W bits cannot hold."""
    fa, fb, fc = reg(8, 3), reg(8, 4), reg(10, 6)
    rng = np.random.default_rng(11)
    M, K, N = 3, 4, 5
    a_shape = (K, M) if adjoint else (M, K)
    ar, br = _draw(rng, fa, a_shape), _draw(rng, fb, (K, N))
    ai, bi = np.full(a_shape, -128, np.int64), np.full((K, N), -128, np.int64)
    got = mm.matmul(ar, ai, fa, br, bi, fb, fc, adjoint=adjoint, form=form)
    want = reference(ar, ai, fa, br, bi, fb, fc, adjoint)
    assert np.array_equal(got[0], want[0]) and np.array_equal(got[1], want[1])


@pytest.mark.parametrize("form", FORMS)
def test_conjugate_of_the_most_negative_value_is_exact(form):
    """conj(-j 2^(W-1)) = +j 2^(W-1): Aᴴ·B with b = j (stored 1) is -2^(W-1), not saturated."""
    fa = fb = reg(8, 3)
    one = np.ones((1, 1), np.int64)
    zero, low = 0 * one, -128 * one
    re, im, _ = mm.matmul_exact(zero, low, fa, zero, one, fb, adjoint=True, form=form)
    assert (int(re[0, 0]), int(im[0, 0])) == (-128, 0)
    re, im, _ = mm.matmul_exact(zero, low, fa, zero, one, fb, form=form)
    assert (int(re[0, 0]), int(im[0, 0])) == (128, 0)


def test_adjoint_equals_the_edge_form():
    """Aᴴ·B = conj(Aᵀ·conj(B)), exact: the form a datapath can use (transpose A at load,
    negate B's imaginary part on the way in and the result's on the way out, before rounding).
    """
    fa, fb = reg(12, 3), reg(12, 4)
    rng = np.random.default_rng(5)
    K, M, N = 6, 4, 7
    ar, ai = _draw(rng, fa, (2, K, M)), _draw(rng, fa, (2, K, M))
    br, bi = _draw(rng, fb, (2, K, N)), _draw(rng, fb, (2, K, N))
    ai[..., 0, :] = bi[..., 0, :] = -(1 << 11)
    re, im, fmt = mm.matmul_exact(ar, ai, fa, br, bi, fb, adjoint=True)
    at = np.swapaxes(ar, -1, -2), np.swapaxes(ai, -1, -2)
    e_re, e_im, _ = mm.matmul_exact(*at, fa, br, -bi, cx.conj_format(fb))
    assert np.array_equal(re, e_re) and np.array_equal(im, -e_im)
    lim = 1 << (fmt.W - 1)
    assert -lim <= min(re.min(), im.min()) and max(re.max(), im.max()) < lim


def test_batch_equals_items():
    fa, fb, fc = reg(12, 3), reg(12, 4), reg(12, 5)
    rng = np.random.default_rng(3)
    ar, ai = _draw(rng, fa, (4, 3, 5)), _draw(rng, fa, (4, 3, 5))
    br, bi = _draw(rng, fb, (5, 2)), _draw(rng, fb, (5, 2))
    re, im = mm.matmul(ar, ai, fa, br, bi, fb, fc)
    for n in range(4):
        r1, i1 = mm.matmul(ar[n], ai[n], fa, br, bi, fb, fc)
        assert np.array_equal(re[n], r1) and np.array_equal(im[n], i1)


def test_acc_format():
    acc = mm.acc_format(reg(12, 3), reg(12, 4), 8)
    assert (acc.W, acc.int_bits, acc.signed) == (28, 11, True)
    assert mm.acc_format(reg(12, 3), reg(12, 4), 9).W == 29


def test_rejects():
    f = reg(8, 3)
    ok = np.zeros((2, 2), np.int64)
    with pytest.raises(ValueError, match="outside"):
        mm.matmul(ok + 128, ok, f, ok, ok, f, f)
    with pytest.raises(ValueError, match="inner dimensions"):
        mm.matmul(
            ok, ok, f, np.zeros((3, 2), np.int64), np.zeros((3, 2), np.int64), f, f
        )
    with pytest.raises(ValueError, match="one shape"):
        mm.matmul(ok, np.zeros((2, 3), np.int64), f, ok, ok, f, f)
    with pytest.raises(NotImplementedError, match="signed"):
        mm.matmul(ok, ok, Format(8, 3, False), ok, ok, f, f)
    with pytest.raises(ValueError, match="form"):
        mm.matmul(ok, ok, f, ok, ok, f, f, form=2)
    wide = Format(32, 4, True)
    with pytest.raises(NotImplementedError):
        mm.matmul(ok, ok, wide, ok, ok, wide, f)
