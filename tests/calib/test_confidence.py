"""The A3 gate: a model reports how much its own prediction should be believed.

The design (``plans/resource_model.md``, decision 6) is a closed sortable level plus a free-form
JSON-able fact dict — deliberately *not* an interval.  Synthesis is deterministic, so there is no noise
process for a prediction interval to estimate; the error that occurs is model misspecification, which
is not measurable from inside the model.  What a model *can* say honestly is which region it was fit
over, whether the query is inside it, and whether its form reproduced the calibration points.

The two properties this file exists to hold down:

* an ``EXACT`` claim is **derived and checked**, never asserted — in particular it is refused for an
  under-determined fit, where zero residual is guaranteed by construction and means nothing;
* ``predict`` stays a bare float on the simulation hot path, and confidence is built only by
  ``estimate`` at reporting time.
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from waveflow.calib.calib import InterpCalibModel, LinCalibModel
from waveflow.calib.confidence import (
    Confidence,
    ConfidenceError,
    ConfidenceLevel,
    Estimate,
    FitSummary,
)


def _affine(n_points: int, *, slope: float = 2.0, intercept: float = 10.0) -> pd.DataFrame:
    """A perfectly affine corpus — zero residual by construction, over *n_points* distinct x."""
    xs = [16 * (i + 1) for i in range(n_points)]
    return pd.DataFrame({"n": xs, "cycles": [slope * x + intercept for x in xs]})


def _model(**kw) -> LinCalibModel:
    return LinCalibModel(basis=["n"], target="cycles", **kw)


# ---------------------------------------------------------------------------
# The level is derived, and EXACT is checked
# ---------------------------------------------------------------------------

def test_overdetermined_exact_fit_reports_exact():
    """An affine law confirmed at more points than it has free parameters is genuinely exact."""
    m = _model().fit(_affine(6))
    conf = m.confidence({"n": 64})          # inside the fitted span
    assert conf.level is ConfidenceLevel.EXACT
    assert conf.facts["max_abs_residual"] == pytest.approx(0.0, abs=1e-9)


def test_leaving_the_sampled_region_outranks_a_confirmed_form():
    """A verified law is still only verified *where it was measured*.

    Fit over n=16..96, query n=100000: the level drops to EXTRAPOLATED, because what breaks a
    confirmed law outside its range is a regime change (a burst-splitting limit, a DSP-vs-LUT
    inference threshold), not a fit error.  The distinction that *does* matter — this is a form that
    held at every point, unlike a noisy fit — rides in the facts, where an agent can weigh it.
    """
    conf = _model().fit(_affine(6)).confidence({"n": 100_000})
    assert conf.level is ConfidenceLevel.EXTRAPOLATED
    assert conf.facts["form_exact"] is True
    assert "regime change" in conf.facts["summary"]


def test_extrapolating_a_noisy_fit_is_distinguishable_from_an_exact_one():
    df = _affine(6)
    df.loc[2, "cycles"] += 7.0
    conf = _model().fit(df).confidence({"n": 100_000})
    assert conf.level is ConfidenceLevel.EXTRAPOLATED
    assert conf.facts["form_exact"] is False


def test_underdetermined_fit_is_refused_exactness():
    """Two points and two free parameters: zero residual is automatic and proves nothing.

    This is the trap in the obvious "fit at n=128 and 256, predict any n" story — the line passes
    through both because it must.
    """
    m = _model().fit(_affine(2))            # x = 16, 32 for a 2-parameter affine form
    conf = m.confidence({"n": 24})          # inside, so the level turns on exactness alone
    assert conf.level is ConfidenceLevel.INTERPOLATED
    assert conf.facts["degenerate_fit"] is True
    assert "not over-determined" in conf.facts["summary"]


def test_out_of_range_reports_extrapolated_and_names_the_knob():
    m = _model().fit(_affine(6))                      # n spans 16..96
    conf = m.confidence({"n": 500})
    assert conf.level is ConfidenceLevel.EXTRAPOLATED
    assert conf.facts["outside"]["n"][0] == 500.0
    assert "n=500 outside" in conf.facts["summary"]


def test_inexact_in_range_fit_reports_interpolated():
    df = _affine(6)
    df.loc[2, "cycles"] += 7.0                        # break exactness, keep the range
    m = _model().fit(df)
    conf = m.confidence({"n": 48})
    assert conf.level is ConfidenceLevel.INTERPOLATED
    assert conf.facts["max_rel_residual"] > 0


def test_extrapolation_wins_over_exactness():
    """A model cannot report EXACT while carrying an out-of-range query — the level is derived."""
    m = _model().fit(_affine(6))
    assert m.confidence({"n": 5}).level is ConfidenceLevel.EXTRAPOLATED


def test_unfitted_model_is_uncalibrated():
    assert _model().confidence({"n": 32}).level is ConfidenceLevel.UNCALIBRATED


def test_seed_backed_model_is_uncalibrated():
    """``load_or_default`` falling back to seed params is silent today; confidence must expose it."""
    m = _model(seed={"n": 2.0, "bias": 10.0}, coeff_names=["n"], intercept_name="bias")
    m.default_model()
    conf = m.confidence({"n": 32})
    assert conf.level is ConfidenceLevel.UNCALIBRATED
    assert conf.facts["from_seed"] is True


# ---------------------------------------------------------------------------
# predict stays a float; estimate carries the confidence
# ---------------------------------------------------------------------------

def test_predict_is_still_a_bare_float():
    """The simulation hot path calls predict per firing and does arithmetic on it."""
    m = _model().fit(_affine(6))
    value = m.predict({"n": 64})
    assert isinstance(value, float)
    assert value == pytest.approx(2.0 * 64 + 10.0)


def test_estimate_pairs_the_value_with_confidence():
    m = _model().fit(_affine(6))
    est = m.estimate({"n": 64}, source="pysim")
    assert isinstance(est, Estimate)
    assert est.value == pytest.approx(m.predict({"n": 64}))
    assert est.source == "pysim"
    assert est.level is ConfidenceLevel.EXACT


def test_estimate_serializes_whole():
    m = _model().fit(_affine(6))
    blob = json.loads(json.dumps(m.estimate({"n": 500}).to_json()))
    assert blob["confidence"]["level"] == "EXTRAPOLATED"
    assert "summary" in blob["confidence"]


# ---------------------------------------------------------------------------
# The Confidence container
# ---------------------------------------------------------------------------

def test_level_is_always_present_in_the_dump():
    """The one guaranteed field — everything else is the model's business."""
    conf = Confidence(level=ConfidenceLevel.EXACT, facts={"anything": [1, 2, 3]})
    assert conf.to_json() == {"level": "EXACT", "anything": [1, 2, 3]}


def test_non_serializable_facts_fail_at_the_model():
    """A stray numpy scalar must blow up here, not at report-dump time far from the cause."""
    import numpy as np

    with pytest.raises(ConfidenceError, match="not JSON-serializable"):
        Confidence(level=ConfidenceLevel.EXACT, facts={"resid": np.float64(0.5), "o": object()})


def test_levels_sort_so_a_report_can_be_triaged():
    levels = [ConfidenceLevel.EXACT, ConfidenceLevel.UNCALIBRATED,
              ConfidenceLevel.EXTRAPOLATED, ConfidenceLevel.INTERPOLATED]
    assert [x.value for x in sorted(levels)] == [
        "UNCALIBRATED", "EXTRAPOLATED", "INTERPOLATED", "EXACT"]
    assert min(levels) is ConfidenceLevel.UNCALIBRATED       # the weakest link, for free


def test_summary_falls_back_when_a_model_gives_none():
    assert "exact" in Confidence(level=ConfidenceLevel.EXACT).summary


# ---------------------------------------------------------------------------
# FitSummary — and its survival into a deployed artifact
# ---------------------------------------------------------------------------

def test_fit_summary_survives_save_and_load(tmp_path):
    """A project that never ran the sweep must still be able to say "you are extrapolating"."""
    path = tmp_path / "params.json"
    trained = _model(path=path).fit(_affine(6))
    trained.save_model()

    deployed = _model(path=path)
    assert deployed.load_model() is not None
    assert deployed.confidence({"n": 500}).level is ConfidenceLevel.EXTRAPOLATED
    assert deployed.confidence({"n": 64}).level is ConfidenceLevel.EXACT


def test_artifact_without_a_summary_still_loads(tmp_path):
    """Additive: an artifact written before fit summaries existed must load unchanged."""
    path = tmp_path / "params.json"
    path.write_text(json.dumps({"n": 2.0, "bias": 10.0}), encoding="utf-8")
    m = _model(path=path, coeff_names=["n"], intercept_name="bias")
    assert m.load_model() is not None
    assert m.predict({"n": 10}) == pytest.approx(30.0)
    assert m.confidence({"n": 10}).level is ConfidenceLevel.UNCALIBRATED


def test_missing_features_are_not_evidence_of_extrapolation():
    """A feature absent from the query is unknown, not out of range."""
    s = FitSummary(ranges={"n": [16, 96], "other": [1, 2]}, n_points=6, n_free_params=2)
    assert s.covers({"n": 32})
    assert s.outside({"n": 32}) == {}


def test_fit_summary_round_trips():
    s = FitSummary(features=["n"], ranges={"n": [16.0, 96.0]}, n_points=6,
                   max_abs_residual=0.0, max_rel_residual=0.0, n_free_params=2)
    assert FitSummary.from_json(s.to_json()) == s


# ---------------------------------------------------------------------------
# InterpCalibModel — a lookup is never "exact", and its clamp is declared
# ---------------------------------------------------------------------------

def _interp() -> InterpCalibModel:
    df = pd.DataFrame({"n_col": [64, 128, 256], "depth": [70.0, 150.0, 260.0]})
    return InterpCalibModel(basis=["n_col"], target="depth").fit(df)


def test_lookup_never_claims_exact():
    """It reproduces its knots because it *is* its knots — the degenerate case exactness excludes."""
    m = _interp()
    conf = m.confidence({"n_col": 128})
    assert conf.level is ConfidenceLevel.INTERPOLATED
    assert conf.facts["model_form"] == "piecewise-linear lookup"


def test_lookup_declares_its_clamp_when_out_of_range():
    """Flat extrapolation is this model's physical claim, but it is still unmeasured territory."""
    conf = _interp().confidence({"n_col": 4096})
    assert conf.level is ConfidenceLevel.EXTRAPOLATED
    assert conf.facts["clamped"] is True
    assert conf.facts["clamped_to"] == 256.0


def test_lookup_estimate_still_predicts_the_clamped_value():
    est = _interp().estimate({"n_col": 4096})
    assert est.value == pytest.approx(260.0)
    assert est.level is ConfidenceLevel.EXTRAPOLATED


# ---------------------------------------------------------------------------
# TimingModel passthrough — what a per-module report actually calls
# ---------------------------------------------------------------------------

def test_timing_model_exposes_confidence_and_tags_the_component(tmp_path):
    """Without the passthrough a report can show a number or nothing, but not a number with
    provenance."""
    from waveflow.calib.timing_model import StreamTimingModel

    tm = StreamTimingModel(component="mem_r_stream_framed_task", calib_dir=tmp_path,
                           features=["nwords", "num_trans"], clk=None)
    conf = tm.confidence({"nwords": 128, "num_trans": 8})
    assert conf.facts["component"] == "mem_r_stream_framed_task"
    assert conf.level is ConfidenceLevel.UNCALIBRATED     # empty dir -> seed fallback

    est = tm.estimate({"nwords": 128, "num_trans": 8})
    assert est.value == pytest.approx(tm.predict({"nwords": 128, "num_trans": 8})[0])
    assert est.source == "pysim"


def test_asking_for_confidence_does_not_change_the_prediction(tmp_path):
    """Confidence loads on the same lazy terms as predict, so it must be side-effect free."""
    from waveflow.calib.timing_model import StreamTimingModel

    tm = StreamTimingModel(component="c", calib_dir=tmp_path,
                           features=["nwords", "num_trans"], clk=None)
    row = {"nwords": 128, "num_trans": 8}
    before = tm.predict(row)
    tm.confidence(row)
    assert tm.predict(row) == before


def test_a_params_only_artifact_reports_uncalibrated_but_says_why(tmp_path):
    """An artifact predating fit summaries still predicts; it just cannot state its support region.

    This is the state the shipped zynq7020 platform components are in today — real fitted
    coefficients, no recorded support — and it must be distinguishable from a seed fallback.
    """
    path = tmp_path / "params.json"
    path.write_text(json.dumps({"n": 2.0, "bias": 10.0}), encoding="utf-8")
    m = _model(path=path, coeff_names=["n"], intercept_name="bias")
    m.load_model()

    conf = m.confidence({"n": 64})
    assert conf.level is ConfidenceLevel.UNCALIBRATED
    assert conf.facts["has_fitted_params"] is True
    assert conf.facts["from_seed"] is False
    assert m.predict({"n": 64}) == pytest.approx(138.0)     # the number is still real
