"""Step 2.3 (and AC2.4's first half) of plans/mimo_cg/mimo_cg_paper_sims.md: the bit-exact CG.

Seeds are fixed here and must never be changed to make a test pass (the plan's Rules 4).
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from examples.mimo_cg.detectors import cg_multi_rhs, mmse_matrix
from examples.mimo_cg.mimo_cg import Config, simulate_point
from examples.mimo_cg.mimo_cg_fixed import (
    INT_BITS,
    CgFormats,
    cg_fixed,
    simulate_point_fixed,
)
from examples.mimo_cg.mimo_link import Qam, noise_variance, point_rng, rayleigh, snr_key

M, K, NS = 64, 8, 32
SEEDS = range(24)


def _system(seed: int, rho_db: float = 0.0, m: int = M, k: int = K):
    """A = HᴴH + σ²I and B = HᴴY for one 16-QAM block, as in the BER runs."""
    rng = point_rng(50, m, k, seed, snr_key(rho_db))
    qam = Qam(16)
    H = rayleigh(rng, (m, k))
    tx = rng.integers(0, 2, size=(NS, k * qam.bits_per_symbol))
    X = qam.modulate(tx).T
    sigma2 = noise_variance(rho_db)
    Y = H @ X + math.sqrt(sigma2) * rayleigh(rng, (m, NS))
    return mmse_matrix(H, sigma2), H.conj().T @ Y


def _rel(x: np.ndarray, ref: np.ndarray) -> float:
    return float(np.linalg.norm(x - ref) / np.linalg.norm(ref))


def test_wide_formats_have_24_fraction_bits_and_fit_64_bits():
    wide = CgFormats.wide()
    assert all(f.frac_bits >= 24 for f in wide.registers().values())
    assert {n: f.int_bits for n, f in wide.registers().items()} == INT_BITS
    widths = {n: f.W for n, f in wide.intermediates(16).items()}
    # The implemented Re(PᴴS) = Σ(p_re·s_re + p_im·s_im) needs 62 bits; gate 2.1 budgeted the
    # conj-then-cmult form at 63, so the record's figure is a safe upper bound.
    assert max(widths.values()) == 62 and widths["Re(P^H S)"] == 62


def test_from_width_follows_the_gate_21_parameterization():
    f = CgFormats.from_width(16, g_s=4, g_div=2)
    for v in ("A", "B", "P", "R", "S", "X"):
        assert getattr(f, v).W == 16 and getattr(f, v).int_bits == INT_BITS[v]
    for s in ("ps", "rz", "alpha", "beta"):
        assert getattr(f, s).W == 20 and getattr(f, s).int_bits == INT_BITS[s]
    assert f.g_div == 2
    with pytest.raises(NotImplementedError):  # past the 64-bit cap
        CgFormats.from_width(32, g_s=4).intermediates(16)


@pytest.mark.parametrize("explicit", [False, True], ids=["recurrence", "explicit"])
def test_wide_formats_tracks_float_cg_at_every_iteration(explicit):
    """AC2.4, first half: X within relative 1e-4 of float CG at every nit <= K, 24 seeds."""
    worst = 0.0
    for seed in SEEDS:
        A, B = _system(seed)
        fixed = cg_fixed(
            A,
            B,
            K,
            CgFormats.wide(),
            scale=M,
            explicit_residual=explicit,
            iterates=range(1, K + 1),
        )
        flt = cg_multi_rhs(
            A, B, K, explicit_residual=explicit, iterates=range(1, K + 1)
        )
        for n in range(1, K + 1):
            err = _rel(fixed[n].real, flt[n])
            worst = max(worst, err)
            assert err <= 1e-4, (seed, n, err)
    assert worst > 0  # it really is fixed point


def test_fixed_cg_is_deterministic_and_batches_exactly():
    systems = [_system(seed) for seed in range(3)]
    A = np.stack([s[0] for s in systems])
    B = np.stack([s[1] for s in systems])
    fmt = CgFormats.from_width(14, g_s=4, g_div=4)
    batched = cg_fixed(A, B, 5, fmt, scale=M)
    again = cg_fixed(A, B, 5, fmt, scale=M)
    assert np.array_equal(batched.re, again.re) and np.array_equal(batched.im, again.im)
    for i in range(3):
        one = cg_fixed(A[i], B[i], 5, fmt, scale=M)
        assert np.array_equal(one.re, batched.re[i]) and np.array_equal(
            one.im, batched.im[i]
        )


def test_zero_rhs_column_freezes_without_disturbing_the_others():
    A, B = _system(0)
    B = B.copy()
    B[:, 3] = 0  # rᴴr = 0 and pᴴAp = 0 in column 3: both divisions hit the zero guard
    regs = []
    out = cg_fixed(
        A,
        B,
        K,
        CgFormats.from_width(16, 4, 4),
        scale=M,
        on_iteration=lambda n, r: regs.append(r),
    )
    assert np.all(out.re[:, 3] == 0) and np.all(out.im[:, 3] == 0)
    assert all(r["alpha"][0][0, 3] == 0 and r["beta"][0][0, 3] == 0 for r in regs)
    ref = cg_fixed(
        A, np.delete(B, 3, axis=1), K, CgFormats.from_width(16, 4, 4), scale=M
    )
    assert np.array_equal(np.delete(out.re, 3, axis=1), ref.re)


def _mean_err(fmt: CgFormats, seeds=range(6)) -> float:
    return float(
        np.mean(
            [
                _rel(
                    cg_fixed(*_system(s), K, fmt, scale=M).real,
                    cg_multi_rhs(*_system(s), K),
                )
                for s in seeds
            ]
        )
    )


def test_narrow_formats_lose_accuracy_monotonically():
    errs = [_mean_err(CgFormats.from_width(W, 4, 4)) for W in (10, 14, 18, 22)]
    assert errs == sorted(errs, reverse=True), errs


def test_scalar_guard_bits_lift_the_quadratic_form_floor():
    """rᴴr and pᴴAp span squared ranges: their integer bits are set early (~85 and ~259), so with
    few fraction bits they lose relative precision as CG converges and α, β stall.  Measured at
    W = 22: g_s = 4 -> 5.9e-4, g_s = 8 -> 1.4e-4 (plan §15).  The largest feasible g_s at W = 22
    is 11; the binding limit is the 64-bit Python model's quantize up-shift for β, not ap_fixed.
    """
    e4 = _mean_err(CgFormats.from_width(22, 4, 4))
    e8 = _mean_err(CgFormats.from_width(22, 8, 4))
    e10 = _mean_err(CgFormats.from_width(22, 10, 4))
    assert e8 * 2 <= e4, (e4, e8)
    assert e10 <= e8 * 1.05, (e8, e10)
    CgFormats.from_width(22, 11, 4).intermediates(16)
    with pytest.raises(NotImplementedError):
        CgFormats.from_width(22, 12, 4).intermediates(16)


def test_the_floor_needs_both_quadratic_forms_widened():
    """M2 review: widening rᴴr alone or pᴴAp alone barely helps; widening both does
    (W = 22: 5.9e-4 -> rz only 4.9e-4, ps only 5.4e-4, both 1.4e-4)."""
    import dataclasses

    from examples.mimo_cg.mimo_cg_fixed import register

    base = CgFormats.from_width(22, 4, 4)

    def wider(fmt):
        return register(fmt.W + 4, fmt.int_bits)

    e_base = _mean_err(base)
    e_rz = _mean_err(dataclasses.replace(base, rz=wider(base.rz)))
    e_ps = _mean_err(dataclasses.replace(base, ps=wider(base.ps)))
    e_both = _mean_err(dataclasses.replace(base, rz=wider(base.rz), ps=wider(base.ps)))
    assert e_both * 2 <= min(e_rz, e_ps) and e_rz > 0.7 * e_base and e_ps > 0.7 * e_base


def test_result_exposes_stored_real_and_typed_views():
    A, B = _system(1)
    out = cg_fixed(A, B, 3, CgFormats.wide(), scale=M)
    da = out.as_data_array()
    assert da.element_type.inner_format() == CgFormats.wide().X
    assert np.array_equal(np.asarray(da.val)["re"], out.re.reshape(-1))
    np.testing.assert_allclose(out.real.real, out.re * 2.0**-out.fmt.frac_bits)


# --- step 2.5: the bit-exact detector in the link simulator --------------------------------

DEFAULT = Config(64, 8, "16qam")


def test_paired_simulator_draws_exactly_the_float_samples():
    """Same budget, no early stop: float-CG error counts equal simulate_point's, error for error."""
    budget = 1_000_000
    base = {
        r["detector"]: r["bit_errors"]
        for r in simulate_point(DEFAULT, 1.0, max_bits=budget, min_errors=10**9)
    }
    rows = simulate_point_fixed(DEFAULT, 1.0, {}, max_bits=budget, min_errors=10**9)
    assert {r["detector"]: r["bit_errors"] for r in rows} == {
        k: v for k, v in base.items() if k.startswith("cg")
    }


@pytest.mark.parametrize("explicit", [False, True], ids=["recurrence", "explicit"])
@pytest.mark.parametrize("rho_db", [-2.0, 0.0, 2.0])
def test_wide_bit_exact_detector_pairs_with_float_cg(rho_db, explicit):
    """AC2.4, second half: on identical samples the wide-format detector's decisions differ from
    float CG's on at most max(5, 1% of float CG's errors) bits, and its error count is within
    max(3, 1%) of float CG's."""
    rows = simulate_point_fixed(
        DEFAULT,
        rho_db,
        {"wide": CgFormats.wide()},
        explicit_residual=explicit,
        max_bits=1_000_000,
        min_errors=10**9,
    )
    by = {r["detector"]: r for r in rows}
    for n in DEFAULT.nits:
        flt, fxd = by[f"cg{n}"], by[f"fx:wide:cg{n}"]
        # Tightened at the M2 review (plan §14): <= max(5, 1% of float CG's errors).
        assert fxd["mismatch"] <= max(5, 0.01 * flt["bit_errors"]), (n, fxd["mismatch"])
        assert abs(fxd["bit_errors"] - flt["bit_errors"]) <= max(
            3, 0.01 * flt["bit_errors"]
        ), (
            n,
            fxd["bit_errors"],
            flt["bit_errors"],
        )
