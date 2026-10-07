"""Step 7.1: the three-multiply complex product equals ``cmult`` bit for bit (Python twin).

Its C++ form, ``complex_utils::cmult3``, is checked against the same values in the complex
conformance harness (``tests/examples/test_complex_conformance.py``, the ``cmult3_*`` cases).
"""

from __future__ import annotations

import numpy as np
import pytest

from waveflow.utils import complexutils as cx
from waveflow.utils.fixputils import Format


def values(fmt: Format, n: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    lo, hi = -(1 << (fmt.W - 1)), (1 << (fmt.W - 1)) - 1
    re, im = rng.integers(lo, hi + 1, n), rng.integers(lo, hi + 1, n)
    edge = [lo, hi, 0, -1, 1]
    k = min(n, len(edge))
    re[:k], im[:k] = edge[:k], edge[::-1][:k]
    return cx.make_complex(re, im, fmt)


@pytest.mark.parametrize(
    "a,b",
    [
        (Format(8, 4, True), Format(8, 4, True)),
        (Format(12, 3, True), Format(12, 4, True)),
        (Format(16, 5, True), Format(10, 2, True)),
        (Format(8, 8, True), Format(8, 8, True)),  # integers
    ],
)
def test_cmult3_equals_cmult(a, b):
    va, vb = values(a, 400, seed=a.W), values(b, 400, seed=b.W + 1)
    want, fw = cx.cmult(va, a, vb, b)
    got, fg = cx.cmult3(va, a, vb, b)
    assert fg == fw == cx.cmult_format(a, b)
    assert np.array_equal(cx.re_of(got), cx.re_of(want))
    assert np.array_equal(cx.im_of(got), cx.im_of(want))


def test_cmult3_refuses_unsigned():
    u = Format(8, 4, False)
    with pytest.raises(NotImplementedError):
        cx.cmult3(
            values(Format(8, 4, True), 2, 0), u, values(Format(8, 4, True), 2, 0), u
        )
