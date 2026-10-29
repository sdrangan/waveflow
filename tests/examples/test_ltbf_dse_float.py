"""examples/ltbf_dse/ltbf_chain.py (plans/ltbf_dse/ltbf_dse.md step 2.3, AC5): the float chain.

AC5 (a) on synthetic statistics is ``test_ltbf_dse_link.py::test_float_cg_converges_to_the_direct_solve``
(copied with the CG code); here it is repeated on the long-term inputs this chain actually builds, from
rendered and estimated SRS.  The drops are local data; those tests skip without them.
"""

from __future__ import annotations

import numpy as np
import pytest

from examples.ltbf_dse import chest as ce
from examples.ltbf_dse import ltbf as lt
from examples.ltbf_dse import ltbf_chain as lc
from examples.ltbf_dse import ltbf_link as lk
from examples.ltbf_dse import scenario as sc
from examples.ltbf_dse import signals as sg
from examples.ltbf_dse.opcount import OpLog

needs_drops = pytest.mark.skipif(
    not (sc.DROPS_DIR / "drop_00001.npz").is_file(),
    reason="the LTBF drops are local data (examples/ltbf_dse/data/drops; plan step 0.4)",
)
TARGET = (-6.0, 14.0)


def _estimated_long_term(n=4, drop_id=1):
    drop = sc.load_drop(drop_id)
    pc0 = lk.power_control(lk.channel(drop, 0, n, lc.BAND), TARGET)
    srs = sg.srs_grid(drop["a0"].shape[1])
    blocks = [
        ce.srs_blocks(
            sg.received(drop, drop_id, 0, n, pc0, srs, s)[:, 0, :], pc0.iconn, OpLog()
        )
        for s in range(drop["yaws"].size)
    ]
    return ce.long_term_from_srs(blocks, pc0, OpLog())


@needs_drops
@pytest.mark.parametrize("form", ["factored", "explicit"])
@pytest.mark.parametrize("regime", [1, 2])
def test_cg_converges_to_the_direct_solve_on_estimated_statistics(form, regime):
    """AC5 (a): the recursion run to convergence (without the paper's absolute stop, as the copied
    test does) reaches a relative residual < 1e-12 per column and the direct solve to 1e-8, on the
    ``Ĥα`` estimated from rendered SRS."""
    ltin = _estimated_long_term()
    for s in range(len(ltin.own)):
        ha = ltin.h_alpha[s]
        if ltin.own[s].size == 0:
            continue
        n = ha.shape[0]
        b = ltin.rhs[s] if regime == 1 else np.eye(n, dtype=complex)
        q = lk.q_hat(ha)
        apply = lt.factored_s(ha) if form == "factored" else lt.explicit_s(ha)
        x = lt.cg_iterates(apply, b, 4 * n, stop=0.0)[-1]
        res = np.linalg.norm(b - q @ x, axis=0) / np.linalg.norm(b, axis=0)
        assert res.max() < 1e-12
        want = np.linalg.solve(q, b)
        assert np.linalg.norm(x - want) <= 1e-8 * np.linalg.norm(want)


def test_whitening_makes_the_projected_covariance_identity():
    """``G' = L⁻¹G`` with ``R̃ = LLᴴ``: ``G'(I + Ĥα,₋ᵢĤα,₋ᵢᴴ)G'ᴴ = I`` (spec §3.4)."""
    rng = np.random.default_rng(1)
    n, cols = 16, 24
    ha = (rng.standard_normal((n, cols)) + 1j * rng.standard_normal((n, cols))) / 2
    g = rng.standard_normal((2, n)) + 1j * rng.standard_normal((2, n))
    gp, _rt = lc.whitened(g, ha, slice(8, 16))
    keep = np.r_[0:8, 16:cols]
    r_i = np.eye(n) + ha[:, keep] @ ha[:, keep].conj().T
    assert np.allclose(gp @ r_i @ gp.conj().T, np.eye(2), atol=1e-10)


def test_measured_sinr_is_the_mmse_sinr():
    """AC5 (b): with the exact weights on a whitened channel ``z' = h'x + n'`` (``n'`` white, unit),
    the measured ``1/mean|x̂ - x|² - 1`` equals ``1/mean(1/(1+γ)) - 1``, ``γ = |h'|²`` a subcarrier,
    within 0.1 dB; the scale-invariant SINR agrees with it."""
    rng = np.random.default_rng(2)
    r = 2
    f = np.arange(sg.N_SC)
    h = (rng.standard_normal((r, 1)) + 1j * rng.standard_normal((r, 1))) * (
        1 + 0.5 * np.cos(2 * np.pi * f / 300)
    )
    gamma = np.sum(np.abs(h) ** 2, axis=0)
    w = h / (1 + gamma)
    reps = 10
    s = len(sg.DATA_SYMS)
    xs, xh = [], []
    for k in range(reps):
        x = sg.qpsk_data(1, seed=k)[0]
        noise = (
            rng.standard_normal((r, s, sg.N_SC))
            + 1j * rng.standard_normal((r, s, sg.N_SC))
        ) / np.sqrt(2)
        z = h[:, None, :] * x[None] + noise
        xs.append(x)
        xh.append(lc.equalize(z, w))
    m = lc.measure(np.concatenate(xh), np.concatenate(xs))
    want = 10 * np.log10(1 / np.mean(1 / (1 + gamma)) - 1)
    assert abs(m["sinr_mmse_db"] - want) < 0.1
    assert abs(m["sinr_scale_inv_db"] - want) < 0.1


@needs_drops
def test_chain_runs_and_is_deterministic():
    """The chain on one drop at 4x4: every served UE measured for the exact and CG configurations,
    and a second run gives the same numbers."""
    kw = {"ranks": (1,), "t_max": 2, "t_eval": (2,)}
    a = lc.run_chain(1, 2, TARGET, **kw)
    b = lc.run_chain(1, 2, TARGET, **kw)
    assert a.served.size > 0
    assert set(a.metrics) == {("exact", 1), ("factored", 1, 2, 1)}
    for key in a.metrics:
        assert set(a.metrics[key]) == set(a.served.tolist())
        assert a.metrics[key] == b.metrics[key]
