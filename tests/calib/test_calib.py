"""Unit tests for waveflow.calib — the bare-bones calibration infrastructure.

Covers the corpus (CalibDataFrame, backed by a pandas DataFrame), the
LinCalibModel fit/predict/score/holdout behaviour (including the through-origin
``span = setup·num_trans + per_word·nwords`` form FIR uses and the basis
generality via caller-side derived columns), and the InterpCalibModel lookup.
"""
from __future__ import annotations

from dataclasses import dataclass

import pandas as pd
import pytest

from waveflow.build.elaborate import elaborate
from waveflow.hw.hw_module import HwModule, HwParam

from waveflow.calib import (
    CalibDataFrame,
    CalibModel,
    InterpCalibModel,
    LinCalibModel,
)
from waveflow.calib.calib import MEASURED_AT


# ---------------------------------------------------------------------------
# CalibDataFrame
# ---------------------------------------------------------------------------

def _grid_db():
    db = CalibDataFrame(["num_trans", "nwords", "span"])
    # span = 5*num_trans + 1*nwords  (exact, through-origin)
    for nt in (1, 2, 4, 8):
        for nw in (64, 256, 1024):
            db.add_datapoint({"num_trans": nt, "nwords": nt * nw, "span": 5 * nt + nt * nw})
    return db


def test_database_add_len_and_df():
    db = _grid_db()
    assert len(db) == 12
    assert isinstance(db.df, pd.DataFrame)
    assert int(db.df.iloc[0]["num_trans"]) == 1
    assert {"num_trans", "nwords", "span"} <= set(db.df.columns)


def test_database_stamps_measured_at():
    db = CalibDataFrame(["a"])
    db.add_datapoint({"a": 1})
    assert MEASURED_AT in db.df.columns
    assert db.df.iloc[0][MEASURED_AT]            # non-empty timestamp
    # measured_at is metadata, never a feature/target — it sorts last in the column order.
    assert list(db.df.columns)[-1] == MEASURED_AT


def test_database_add_copies_input():
    db = CalibDataFrame(["a"])
    d = {"a": 1}
    db.add_datapoint(d)
    d["a"] = 999
    assert int(db.df.iloc[0]["a"]) == 1   # stored a copy, not a reference


def test_database_native_pandas_filter_and_column():
    db = _grid_db()
    small = db.df[db.df.num_trans <= 2]
    assert len(small) == 6
    assert set(db.df["num_trans"]) == {1, 2, 4, 8}


def test_database_column_order():
    db = _grid_db()
    assert list(db.df.columns)[:3] == ["num_trans", "nwords", "span"]
    assert len(db.df) == 12


def test_database_save_load_roundtrip(tmp_path):
    db = _grid_db()
    p = db.save(tmp_path / "corpus.csv")
    assert p.exists()
    db2 = CalibDataFrame.load(p)
    assert len(db2) == 12
    assert list(db2.df["span"]) == list(db.df["span"])


# ---------------------------------------------------------------------------
# LinCalibModel — through-origin linear (the FIR form)
# ---------------------------------------------------------------------------

def test_linear_through_origin_recovers_rates():
    db = _grid_db()   # span = 5*num_trans + 1*nwords
    m = LinCalibModel(["num_trans", "nwords"], "span", fit_intercept=False).fit(db)
    c = m.coeffs
    assert c["num_trans"] == pytest.approx(5.0, abs=1e-6)
    assert c["nwords"] == pytest.approx(1.0, abs=1e-6)
    assert "intercept" not in c
    assert m.score(db) == pytest.approx(1.0, abs=1e-9)
    assert m.predict_feat({"num_trans": 3, "nwords": 300}) == pytest.approx(315.0)


def test_linear_with_intercept():
    db = CalibDataFrame(["x", "y"])
    for x in range(10):
        db.add_datapoint({"x": x, "y": 7 + 3 * x})
    m = LinCalibModel(["x"], "y", fit_intercept=True).fit(db)
    assert m.coeffs["x"] == pytest.approx(3.0)
    assert m.coeffs["intercept"] == pytest.approx(7.0)


def test_predict_requires_fit():
    m = LinCalibModel(["x"], "y")
    with pytest.raises(RuntimeError):
        m.predict_feat({"x": 1})


def test_basis_generality_via_derived_column():
    # A genuinely concave target the pure-linear basis cannot fit but a sqrt basis can.
    # The transform is a caller-side derived column on the DataFrame — pandas-idiomatic.
    db = CalibDataFrame(["n_col", "t"])
    for nc in (64, 256, 1024, 4096):
        db.add_datapoint({"n_col": nc, "t": 10.0 + 3.0 * nc ** 0.5})
    db.df["sqrt_nc"] = db.df.n_col ** 0.5
    lin = LinCalibModel(["n_col"], "t").fit(db)
    sqrtm = LinCalibModel(["sqrt_nc"], "t", fit_intercept=True).fit(db)
    assert sqrtm.score(db) == pytest.approx(1.0, abs=1e-9)
    assert sqrtm.score(db) > lin.score(db)   # the right basis wins


def test_as_dict_serializable():
    db = _grid_db()
    m = LinCalibModel(["num_trans", "nwords"], "span", fit_intercept=False).fit(db)
    d = m.as_dict()
    assert d["target"] == "span"
    assert d["basis"] == ["num_trans", "nwords"]
    assert "num_trans" in d["coeffs"] and "nwords" in d["coeffs"]


# ---------------------------------------------------------------------------
# Model-owned transform + load_coeffs deploy + clock-independence (the FIR form)
# ---------------------------------------------------------------------------

def _compute_features(params):
    """FIR compute-time features: clk_period folded in -> predict returns SECONDS and the
    coefficients are cycle-domain rates (row_setup, compute_beat).

    Takes the **parameter row** and returns a *named* mapping, so a feature and the coefficient that
    multiplies it are the same name.  It never sees a component — that is what guarantees every
    input is one the corpus recorded.
    """
    cp, nr, nc = float(params["clk_period"]), float(params["n_row"]), float(params["n_col"])
    return {"row_setup": cp * (nr - 1.0), "compute_beat": cp * nr * nc}


def test_model_owned_transform_and_clock_independence():
    # Ground truth: 8 cyc/row-boundary + 1 cyc/input-sample, measured at clk_period = 10 ns.
    CP_FIT = 10e-9
    db = CalibDataFrame(["n_row", "n_col", "clk_period", "compute_time"])
    for nr in (1, 4, 16):
        for nc in (16, 64, 256):
            cyc = 8.0 * (nr - 1) + 1.0 * nr * nc
            db.add_datapoint({"n_row": nr, "n_col": nc, "clk_period": CP_FIT,
                              "compute_time": cyc * CP_FIT})
    m = LinCalibModel(["row_setup", "compute_beat"], "compute_time",
                      fit_intercept=False, transform_fn=_compute_features).fit(db)
    # through-origin coefficients recover the cycle-domain rates
    assert m.coeffs["row_setup"] == pytest.approx(8.0, abs=1e-6)
    assert m.coeffs["compute_beat"] == pytest.approx(1.0, abs=1e-6)
    # predict at a DIFFERENT clock with no re-fit: cycles are clock-independent, time scales
    cyc_4x64 = 8.0 * 3 + 1.0 * 4 * 64          # = 280 cycles
    assert m.predict_feat({"n_row": 4, "n_col": 64, "clk_period": 10e-9}) == pytest.approx(cyc_4x64 * 10e-9)
    assert m.predict_feat({"n_row": 4, "n_col": 64, "clk_period": 2e-9}) == pytest.approx(cyc_4x64 * 2e-9)


def test_load_params_deploy_roundtrip():
    # A deployed model (from stored params) predicts identically — no fit / training data needed.
    fitted = LinCalibModel([], "compute_time", fit_intercept=False, transform_fn=_compute_features,
                           coeff_names=["row_setup", "compute_beat"])
    fitted.load_params({"row_setup": 8.0, "compute_beat": 1.0})
    row = {"n_row": 4, "n_col": 64, "clk_period": 10e-9}
    assert fitted.predict_feat(row) == pytest.approx(280 * 10e-9)
    # a bias-only-style through-origin fill model: fill_time = clk_period * L0
    fill = LinCalibModel([], "fill_time", fit_intercept=False, coeff_names=["L0"],
                         transform_fn=lambda p: {"L0": float(p["clk_period"])}).load_params({"L0": 60.0})
    assert fill.predict_feat({"clk_period": 10e-9}) == pytest.approx(60 * 10e-9)


def test_to_params_named_vs_vector():
    # coeff_names -> individually named state_dict; None -> flat "coeffs" vector.
    db = _grid_db()
    named = LinCalibModel(["num_trans", "nwords"], "span", fit_intercept=False,
                          coeff_names=["setup", "per_word"]).fit(db)
    assert named.to_params() == {"setup": pytest.approx(5.0), "per_word": pytest.approx(1.0)}
    vec = LinCalibModel(["num_trans", "nwords"], "span", fit_intercept=False).fit(db)
    p = vec.to_params()
    assert set(p) == {"coeffs"} and p["coeffs"] == pytest.approx([5.0, 1.0])
    # intercept goes under intercept_name only when fit_intercept
    biased = LinCalibModel(["x"], "y", fit_intercept=True, intercept_name="L0")
    biased.load_params({"coeffs": [3.0], "L0": 7.0})
    assert biased.predict_feat({"x": 2}) == pytest.approx(13.0)


def test_save_load_default_artifact(tmp_path):
    # save_model -> load_model round-trip through a file; load_or_default falls back to the seed.
    path = tmp_path / "m.json"
    seed = {"row_setup": 8.0, "compute_beat": 1.0}
    m = LinCalibModel([], "compute_time", fit_intercept=False, transform_fn=_compute_features,
                      coeff_names=["row_setup", "compute_beat"], seed=seed, path=path)
    # no file yet -> load_or_default uses the seed
    assert m.load_or_default().predict_feat({"n_row": 4, "n_col": 64, "clk_period": 10e-9}) \
        == pytest.approx(280 * 10e-9)
    # fit a DIFFERENT model, save it, and confirm a fresh shell loads those params from the file
    CP = 10e-9
    db = CalibDataFrame(["n_row", "n_col", "clk_period", "compute_time"])
    for nr in (1, 4, 16):
        for nc in (16, 64, 256):
            db.add_datapoint({"n_row": nr, "n_col": nc, "clk_period": CP,
                              "compute_time": (5.0 * (nr - 1) + 2.0 * nr * nc) * CP})
    m.fit(db).save_model()
    assert path.exists()
    fresh = LinCalibModel([], "compute_time", fit_intercept=False, transform_fn=_compute_features,
                          coeff_names=["row_setup", "compute_beat"], seed=seed, path=path)
    fresh.load_or_default()   # file exists -> loads fitted params, not the seed
    assert fresh.coeffs["row_setup"] == pytest.approx(5.0, abs=1e-6)
    assert fresh.coeffs["compute_beat"] == pytest.approx(2.0, abs=1e-6)


# ---------------------------------------------------------------------------
# Metrics / holdout
# ---------------------------------------------------------------------------

def test_rel_errors_skip_zero_actual():
    db = CalibDataFrame(["x", "y"])
    db.add_datapoint({"x": 0, "y": 0})
    db.add_datapoint({"x": 1, "y": 10})
    db.add_datapoint({"x": 2, "y": 20})
    m = LinCalibModel(["x"], "y", fit_intercept=False).fit(db)
    errs = m.rel_errors(db)
    assert len(errs) == 2          # the y==0 row is skipped
    assert max(errs) < 1e-9


def test_holdout_report_structure():
    db = _grid_db()
    train = db.df[~((db.df.num_trans == 2) & (db.df.nwords == 512))]
    test = db.df[(db.df.num_trans == 2) & (db.df.nwords == 512)]
    m = LinCalibModel(["num_trans", "nwords"], "span", fit_intercept=False)
    rep = m.holdout_report(train, test)
    assert rep["target"] == "span"
    assert rep["r2_train"] == pytest.approx(1.0, abs=1e-9)
    assert len(rep["test"]) == 1
    assert rep["test"][0]["rel_err"] == pytest.approx(0.0, abs=1e-9)
    assert rep["max_rel_err"] == pytest.approx(0.0, abs=1e-9)


def test_score_base_predict_not_implemented():
    # The base CalibModel is abstract: fit/_predict_one must be provided.
    m = CalibModel(["x"], "y")
    with pytest.raises(NotImplementedError):
        m.fit([])
    with pytest.raises(RuntimeError):
        m.predict_feat({"x": 1})   # not fitted


def test_interp_model_lookup_and_saturation():
    # A saturating curve (like the per-row pipeline depth row_depth(n_col)).
    db = CalibDataFrame(["n_col", "row_depth"])
    for nc, rd in [(64, 70.0), (256, 260.0), (1024, 268.0)]:
        db.add_datapoint({"n_col": nc, "row_depth": rd})
    m = InterpCalibModel(["n_col"], "row_depth").fit(db)
    # exact at samples
    assert m.predict_feat({"n_col": 256}) == pytest.approx(260.0)
    # linear interpolation between samples (untrained n_col)
    assert m.predict_feat({"n_col": 160}) == pytest.approx(70.0 + (260.0 - 70.0) * (160 - 64) / (256 - 64))
    # flat extrapolation beyond the range (the saturation)
    assert m.predict_feat({"n_col": 4096}) == pytest.approx(268.0)
    assert m.predict_feat({"n_col": 1}) == pytest.approx(70.0)


def test_interp_model_averages_duplicates():
    # row_depth measured at several n_row for the same n_col -> averaged.
    db = CalibDataFrame(["n_col", "row_depth"])
    for rd in (69.0, 70.0, 71.0):
        db.add_datapoint({"n_col": 256, "row_depth": rd})
    db.add_datapoint({"n_col": 1024, "row_depth": 268.0})
    m = InterpCalibModel(["n_col"], "row_depth").fit(db)
    assert m.predict_feat({"n_col": 256}) == pytest.approx(70.0)


def test_interp_model_roundtrip_samples():
    db = CalibDataFrame(["x", "y"])
    for x, y in [(1, 10.0), (2, 12.0), (4, 13.0)]:
        db.add_datapoint({"x": x, "y": y})
    m = InterpCalibModel(["x"], "y").fit(db)
    s = m.samples
    assert s["feature"] == "x" and s["x"] == [1.0, 2.0, 4.0] and s["y"] == [10.0, 12.0, 13.0]
    m2 = InterpCalibModel.from_samples("x", s["x"], s["y"], "y")
    assert m2.predict_feat({"x": 3}) == pytest.approx(m.predict_feat({"x": 3}))


def test_interp_model_rejects_multifeature():
    with pytest.raises(ValueError):
        InterpCalibModel(["a", "b"], "y")


def test_measured_at_never_leaks_into_fit():
    # A model selects explicit basis/target columns, so the measured_at metadata
    # column can never appear in the design matrix.
    db = _grid_db()
    m = LinCalibModel(["num_trans", "nwords"], "span", fit_intercept=False).fit(db)
    assert MEASURED_AT not in m.basis
    assert m.target != MEASURED_AT
    # design matrix is exactly the basis columns
    assert m.design(db.df).shape == (12, 2)


def test_plot_smoke():
    pytest.importorskip("matplotlib")
    import matplotlib
    matplotlib.use("Agg")
    db = _grid_db()
    m = LinCalibModel(["num_trans", "nwords"], "span", fit_intercept=False).fit(db)
    ax = m.plot(db, x_name="nwords")
    assert ax is not None


# ---------------------------------------------------------------------------
# The component-facing layer (P1 of plans/harmonize_calib.md)
# ---------------------------------------------------------------------------

@dataclass(kw_only=True)
class _ParamBlk(HwModule):
    """Module-level on purpose: with ``from __future__ import annotations`` a *function-local*
    class keeps its ``HwParam[int]`` annotations as unresolvable strings, so no parameter is
    discovered and every transform comes back empty."""

    width: HwParam[int] = 16
    depth: HwParam[int] = 64


def test_get_params_defaults_to_the_resolved_parameters():
    """The default extraction is the identity — every resolved HwParam, by name."""
    comp = elaborate(_ParamBlk, {"width": 32, "depth": 128}, name="b")
    assert (LinCalibModel(basis=["width"], target="y").get_params(comp)
            == {"width": 32, "depth": 128})


def test_get_params_folds_in_runtime_inputs():
    """Timing predicts from design **and workload**, so the extraction takes ``**runtime``.

    A resource model drops them; a timing model consumes them.  This is the one place the shared
    base is shaped by timing, and it costs resource nothing.
    """
    comp = elaborate(_ParamBlk, {"width": 16, "depth": 64}, name="b")
    got = LinCalibModel(basis=["nwords"], target="y").get_params(comp, nwords=256, num_trans=4)
    assert got == {"width": 16, "depth": 64, "nwords": 256, "num_trans": 4}


def test_transform_cannot_see_the_component():
    """The guarantee: a feature can only be built from what `get_params` recorded.

    `transform` takes the parameter mapping, so a model physically cannot predict from a fact the
    corpus does not hold — which is what makes a stored corpus re-fittable.
    """
    import inspect

    sig = inspect.signature(LinCalibModel.transform)
    assert list(sig.parameters) == ["self", "params"]


def test_targets_reports_the_single_target():
    assert LinCalibModel(basis=["n"], target="residual").targets == ("residual",)


def test_name_defaults_to_the_target():
    assert LinCalibModel(basis=["n"], target="residual").name == "residual"


def test_paths_derive_from_the_platform_and_name():
    """Three hand-rolled storage schemes collapse into one derivation."""
    from pathlib import Path

    from waveflow.calib.platform import Platform

    plat = Platform(name="z20", dir=Path("/plat/z20"), part="xc7z020clg484-1", clk_freq=100e6)
    m = LinCalibModel(basis=["n"], target="residual", name="mem_r_span", platform=plat)
    assert m.data_dir.as_posix() == "/plat/z20/models/mem_r_span"
    assert m.corpus_path.name == "corpus.csv"
    assert m.params_path.name == "params.json"


def test_paths_are_none_without_a_platform():
    """A model with no platform still works — it simply has nowhere derived to store anything."""
    m = LinCalibModel(basis=["n"], target="residual")
    assert m.data_dir is None and m.corpus_path is None and m.params_path is None
