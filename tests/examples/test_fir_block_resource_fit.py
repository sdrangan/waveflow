"""The D2 gate: the LUT/FF model, validated **held-out**, and honest about being the weaker half.

DSP and BRAM are binding decisions and their prior is exact (``test_fir_block_resource_prior.py``).
LUT and FF are the genuinely estimated counters — partitioned storage, pipeline registers, the
accumulate tree, address and mux logic — so they are fitted, and the number that matters is error on
points the fit never saw.

Everything here runs against the committed corpus (``fir_block_corpus.py``), so it needs no toolchain.
Tolerances are set from what leave-one-out actually produced, deliberately close to the measured
figures: a loose bound would pass for years without noticing the model had rotted.
"""
from __future__ import annotations

import numpy as np
import pytest

from waveflow.build.elaborate import elaborate
from examples.fir_block.fir_block import FirCompute
from examples.fir_block.fir_block_corpus import GRID, points
from examples.fir_block.fir_block_resource import (
    FITTED_BASIS,
    fir_compute_basis,
    fir_compute_fitted,
)

MEM_DW = 32


def _comp(ntap, samp_w, unroll):
    return elaborate(FirCompute, {"mem_dwidth": MEM_DW, "ntap": ntap, "samp_w": samp_w,
                                  "samp_i": 2, "unroll_lane": unroll}, name="fir_compute")


def _samples(exclude=None):
    out = []
    for n, w, u, m in points():
        if exclude is not None and (n, w, u) == exclude:
            continue
        out.append((_comp(n, w, u), m))
    return out


@pytest.fixture(scope="module")
def fitted():
    return fir_compute_fitted().fit(_samples())


# ---------------------------------------------------------------------------
# Held-out error — the number that matters
# ---------------------------------------------------------------------------

def _leave_one_out():
    """Refit without each point and predict it.  Returns {counter: [rel errors]}."""
    errs = {"lut": [], "ff": []}
    for n, w, u, measured in points():
        model = fir_compute_fitted().fit(_samples(exclude=(n, w, u)))
        pred = model.predict_own(_comp(n, w, u))
        for c in errs:
            errs[c].append(abs(pred[c] - measured[c]) / measured[c])
    return errs


@pytest.fixture(scope="module")
def loo():
    return _leave_one_out()


def test_ff_holds_out_well(loo):
    """FF tracks storage bits closely — partitioned arrays become registers."""
    assert np.mean(loo["ff"]) < 0.08, f"FF mean held-out error {np.mean(loo['ff']):.1%}"
    assert max(loo["ff"]) < 0.20, f"FF worst held-out error {max(loo['ff']):.1%}"


def test_lut_is_the_weaker_counter_and_the_bound_says_so(loo):
    """LUT is the honest limit of this approach: ~10% mean, ~25% worst, held out.

    Recorded as a *bound the model must stay inside*, not as a success. It is the reason validation
    leads with decision fidelity rather than relative error — a 25% LUT error still picks the right
    design when candidates are well separated, and that is the claim worth making.
    """
    assert np.mean(loo["lut"]) < 0.13, f"LUT mean held-out error {np.mean(loo['lut']):.1%}"
    assert max(loo["lut"]) < 0.30, f"LUT worst held-out error {max(loo['lut']):.1%}"


def test_ff_beats_lut_which_is_the_expected_ordering(loo):
    """Storage is nearly analytic; combinational logic is not. If this ever inverts, something moved."""
    assert np.mean(loo["ff"]) < np.mean(loo["lut"])


# ---------------------------------------------------------------------------
# The model behaves like a model
# ---------------------------------------------------------------------------

def test_predicts_every_counter_it_claims(fitted):
    """The prior rides inside the fitted model, so one object answers for all four counters."""
    out = fitted.predict_own(_comp(32, 16, False))
    assert set(out) == {"lut", "ff", "dsp", "bram"}
    assert out["lut"] > 0 and out["ff"] > 0
    assert out["dsp"] == 32 and out["bram"] == 0        # from the prior, exact


def test_in_sample_predictions_are_close(fitted):
    """Not the real test — but a fit that cannot reproduce its own training data is broken."""
    for n, w, u, m in points():
        pred = fitted.predict_own(_comp(n, w, u))
        assert abs(pred["ff"] - m["ff"]) / m["ff"] < 0.20


def test_unfitted_model_reports_uncalibrated():
    from waveflow.calib.confidence import ConfidenceLevel

    conf = fir_compute_fitted().confidence_own(_comp(32, 16, False))
    assert conf.level is ConfidenceLevel.UNCALIBRATED
    assert "not been fitted" in conf.summary


def test_confidence_is_per_counter_and_takes_the_worst(fitted):
    conf = fitted.confidence_own(_comp(32, 16, False))
    assert set(conf.facts["per_counter"]) == {"lut", "ff"}
    assert conf.level.rank <= min(
        __import__("waveflow.calib.confidence", fromlist=["ConfidenceLevel"])
        .ConfidenceLevel(c["level"]).rank for c in conf.facts["per_counter"].values())


def test_extrapolation_beyond_the_grid_is_reported(fitted):
    """``ntap=256`` is far outside the fitted 8..32 — the model must say so, not quietly answer."""
    from waveflow.calib.confidence import ConfidenceLevel

    conf = fitted.confidence_own(_comp(256, 16, False))
    assert conf.level is ConfidenceLevel.EXTRAPOLATED
    assert "outside" in conf.summary


def test_inside_the_grid_is_not_flagged_as_extrapolation(fitted):
    from waveflow.calib.confidence import ConfidenceLevel

    assert fitted.confidence_own(_comp(16, 16, False)).level is not ConfidenceLevel.EXTRAPOLATED


# ---------------------------------------------------------------------------
# The features, which are the actual design decision
# ---------------------------------------------------------------------------

def test_features_are_structural_not_raw_parameters():
    f = fir_compute_basis(_comp(32, 16, True))
    assert f["lw"] == 2                       # 32 // 16
    assert f["n_mult"] == 32 * 2              # unrolled: NTAP * LW
    assert f["acc_bits"] == 2 * 16 + 5        # 2W + ceil(log2 32)
    assert f["store_bits"] == 16 * (32 + (32 + 2 - 1))   # taps + a lane-extended delay line


def test_serial_and_unrolled_differ_in_the_features_not_in_the_model():
    """Pooling across realizations only works because the features carry the difference."""
    ser = fir_compute_basis(_comp(32, 16, False))
    unr = fir_compute_basis(_comp(32, 16, True))
    assert ser["n_mult"] != unr["n_mult"]
    assert ser["store_bits"] != unr["store_bits"]


def test_basis_is_declared_per_counter():
    assert FITTED_BASIS["ff"] == ["store_bits", "n_mult"]
    assert "mac_bits" in FITTED_BASIS["lut"]


def test_corpus_is_the_full_grid():
    assert len(GRID) == 24
    assert {u for _, _, u in GRID} == {False, True}
