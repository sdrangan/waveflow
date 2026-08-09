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
    fir_compute_model,
)

MEM_DW = 32


def _comp(ntap, samp_w, unroll):
    return elaborate(FirCompute, {"mem_dwidth": MEM_DW, "ntap": ntap, "samp_w": samp_w,
                                  "samp_i": 2, "unroll_lane": unroll}, name="fir_compute")


def _params(ntap, samp_w, unroll):
    """The parameter row a corpus would hold — what `fir_compute_basis` derives from.

    It takes params rather than a component precisely so that a fit reads the same shape a corpus
    stores; see `docs/guide/calib/model.md`.
    """
    from waveflow.calib.module_key import identify_instance

    return dict(identify_instance(_comp(ntap, samp_w, unroll), require_bound=False).params)


def _samples(exclude=None):
    out = []
    for n, w, u, m in points():
        if exclude is not None and (n, w, u) == exclude:
            continue
        out.append((_comp(n, w, u), m))
    return out


@pytest.fixture(scope="module")
def fitted():
    return fir_compute_model().fit(_samples())


# ---------------------------------------------------------------------------
# Held-out error — the number that matters
# ---------------------------------------------------------------------------

def _leave_one_out():
    """Refit without each point and predict it.  Returns {counter: [rel errors]}."""
    errs = {"lut": [], "ff": []}
    for n, w, u, measured in points():
        model = fir_compute_model().fit(_samples(exclude=(n, w, u)))
        pred = model.predict(_comp(n, w, u))
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
    """One object answers for every counter, each from whichever half is honest for it.

    ``uram`` and ``srl`` are present and zero rather than absent, which is the difference that
    matters: an omitted counter contributes silently and makes a design read as cheaper than it is.
    """
    out = fitted.predict(_comp(32, 16, False))
    assert set(out) == {"lut", "ff", "dsp", "bram", "uram", "srl"}
    assert out["lut"] > 0 and out["ff"] > 0             # regressed
    assert out["dsp"] == 32 and out["bram"] == 0        # derived from structure, exact
    assert out["uram"] == 0 and out["srl"] == 0         # predicted zero, not omitted


def test_in_sample_predictions_are_close(fitted):
    """Not the real test — but a fit that cannot reproduce its own training data is broken."""
    for n, w, u, m in points():
        pred = fitted.predict(_comp(n, w, u))
        assert abs(pred["ff"] - m["ff"]) / m["ff"] < 0.20


def test_unfitted_model_reports_uncalibrated():
    from waveflow.calib.confidence import ConfidenceLevel

    conf = fir_compute_model().confidence(_comp(32, 16, False))
    assert conf.level is ConfidenceLevel.UNCALIBRATED
    assert "not been fitted" in conf.summary


def test_confidence_is_per_counter_and_takes_the_worst(fitted):
    """Every counter reports its own level, and the composite one is the weakest of them.

    The derived counters are `EXACT` and the regressed ones are not, so this also pins that a model
    which is exact on half its counters does not get to claim `EXACT` overall.
    """
    from waveflow.calib.confidence import ConfidenceLevel

    conf = fitted.confidence(_comp(32, 16, False))
    per = conf.facts["per_target"]
    assert {"lut", "ff", "dsp", "bram"} <= set(per)
    assert ConfidenceLevel(per["dsp"]["level"]) is ConfidenceLevel.EXACT
    assert conf.level.rank <= min(ConfidenceLevel(c["level"]).rank for c in per.values())
    assert conf.level is not ConfidenceLevel.EXACT


def test_extrapolation_beyond_the_grid_is_reported(fitted):
    """``ntap=256`` is far outside the fitted 8..32 — the model must say so, not quietly answer.

    Checked against the structured `outside` rather than a substring of the summary: it names the
    quantity, its value and the fitted range, so a report can say *which* knob left the region and
    by how far. `ntap` is the knob the caller turned; `n_mult` is the structure that actually grew.
    """
    from waveflow.calib.confidence import ConfidenceLevel

    conf = fitted.confidence(_comp(256, 16, False))
    assert conf.level is ConfidenceLevel.EXTRAPOLATED
    outside = conf.facts["per_target"]["lut"]["outside"]
    assert outside["ntap"][0] == 256                       # value asked for
    assert outside["ntap"][1:] == [8.0, 32.0]              # fitted range it left
    assert "basis_n_mult" in outside                       # and the basis term that followed it


def test_inside_the_grid_is_not_flagged_as_extrapolation(fitted):
    from waveflow.calib.confidence import ConfidenceLevel

    assert fitted.confidence(_comp(16, 16, False)).level is not ConfidenceLevel.EXTRAPOLATED


# ---------------------------------------------------------------------------
# The features, which are the actual design decision
# ---------------------------------------------------------------------------

def _terms(ntap, samp_w, unroll):
    """The declared basis terms — what the module says, not what a separate transform derives."""
    return _comp(ntap, samp_w, unroll).resource_structure().basis_terms()


def test_features_are_structural_not_raw_parameters():
    """The declared terms are quantities of hardware, not the knobs the caller turned.

    ``ntap`` and ``samp_w`` are nowhere in the basis: what the fit sees is how many multipliers were
    instantiated and how many bits of state, which is why one basis spans both realizations.
    """
    f = _terms(32, 16, True)
    assert f["n_mult"] == 32 * 2                          # unrolled: NTAP * LW, LW = 32 // 16
    assert f["store_bits"] == 16 * (32 + (32 + 2 - 1))    # taps + a lane-extended delay line
    assert f["mac_bits"] == 32 * 2 * (2 * 16 + 5)         # n_mult * (2W + ceil(log2 NTAP))
    assert "ntap" not in f and "samp_w" not in f


def test_serial_and_unrolled_differ_in_the_features_not_in_the_model():
    """Pooling across realizations only works because the features carry the difference."""
    ser, unr = _terms(32, 16, False), _terms(32, 16, True)
    assert ser["n_mult"] != unr["n_mult"]
    assert ser["store_bits"] != unr["store_bits"]


def test_the_declared_multipliers_reproduce_the_dsp_oracle():
    """The structure declaration and the corpus's prior must agree — two statements of one law.

    ``dsp_prior`` stays as a test oracle (it is what the measurements were checked against); the
    model no longer calls it, deriving DSP from the declared ``MultGroup`` rows instead. This is what
    keeps the two from drifting.
    """
    from waveflow.calib.device_rules import dsp_count
    from examples.fir_block.fir_block_resource import PART, dsp_prior

    for ntap, samp_w, unroll, _ in points():
        mults = _comp(ntap, samp_w, unroll).resource_structure().multipliers
        derived = sum(dsp_count(g.count, g.operand_bits, PART) for g in mults)
        assert derived == dsp_prior(_params(ntap, samp_w, unroll)), (ntap, samp_w, unroll)


def test_basis_is_declared_per_counter():
    assert FITTED_BASIS["ff"] == ["store_bits", "n_mult"]
    assert "mac_bits" in FITTED_BASIS["lut"]


def test_corpus_is_the_full_grid():
    assert len(GRID) == 24
    assert {u for _, _, u in GRID} == {False, True}
