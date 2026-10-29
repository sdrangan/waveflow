"""examples/ltbf_dse/opcount.py (plans/ltbf_dse/ltbf_dse.md step 2.2): results and counts."""

from __future__ import annotations

import numpy as np

from examples.ltbf_dse.opcount import ELEM_BYTES, OpLog


def _c(rng, *shape):
    return rng.standard_normal(shape) + 1j * rng.standard_normal(shape)


def test_matmul_counts_m_k_n_and_computes():
    rng = np.random.default_rng(1)
    a, b = _c(rng, 6, 5), _c(rng, 5, 3)
    log = OpLog(stage="s")
    assert np.allclose(log.matmul(a, b), a @ b)
    assert np.allclose(log.matmul(_c(rng, 5, 6), b, conj_a=True).shape, (6, 3))
    t = log.totals("s")
    assert t["cmacs"] == 6 * 5 * 3 * 2
    assert t["bytes_out"] == 2 * 6 * 3 * ELEM_BYTES


def test_elementwise_divide_sqrt_and_stages():
    rng = np.random.default_rng(2)
    log = OpLog(stage="a")
    x = _c(rng, 4, 7)
    log.mul(x, x, conj_b=True)
    log.sumsq(x, axis=0)
    log.stage = "b"
    d = log.divide(x, np.full((4, 7), 2.0))
    log.sqrt(np.ones(3))
    assert np.allclose(d, x / 2)
    assert log.totals("a") == {
        "cmacs": 56,
        "divides": 0,
        "sqrts": 0,
        "bytes_in": (56 + 28) * ELEM_BYTES,
        "bytes_out": (28 + 7) * ELEM_BYTES,
    }
    assert log.totals("b")["divides"] == 28 and log.totals("b")["sqrts"] == 3
    assert list(log.by_stage()) == ["a", "b"]
