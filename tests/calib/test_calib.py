"""Unit tests for waveflow.calib — the bare-bones calibration infrastructure.

Covers the corpus (CalibDatabase), the Feature transform layer, and the
LinCalibModel fit/predict/score/holdout behaviour, including the through-origin
``span = setup·num_trans + per_word·nwords`` form FIR uses and the basis-function
generality (e.g. a sqrt transform) the class must support.
"""
from __future__ import annotations

import numpy as np
import pytest

from waveflow.calib import (
    CalibDatabase,
    CalibModel,
    Feature,
    InterpCalibModel,
    LinCalibModel,
)


# ---------------------------------------------------------------------------
# CalibDatabase
# ---------------------------------------------------------------------------

def _grid_db():
    db = CalibDatabase(["num_trans", "nwords", "span"])
    # span = 5*num_trans + 1*nwords  (exact, through-origin)
    for nt in (1, 2, 4, 8):
        for nw in (64, 256, 1024):
            db.add_datapoint({"num_trans": nt, "nwords": nt * nw, "span": 5 * nt + nt * nw})
    return db


def test_database_add_len_iter():
    db = _grid_db()
    assert len(db) == 12
    rows = list(db)
    assert rows[0]["num_trans"] == 1
    assert all({"num_trans", "nwords", "span"} <= set(r) for r in rows)


def test_database_add_copies_input():
    db = CalibDatabase(["a"])
    d = {"a": 1}
    db.add_datapoint(d)
    d["a"] = 999
    assert db.rows[0]["a"] == 1   # stored a copy, not a reference


def test_database_filter_and_column():
    db = _grid_db()
    small = db.filter(lambda r: r["num_trans"] <= 2)
    assert len(small) == 6
    assert set(db.column("num_trans")) == {1.0, 2.0, 4.0, 8.0}


def test_database_display_is_dataframe():
    pd = pytest.importorskip("pandas")
    df = _grid_db().display()
    assert isinstance(df, pd.DataFrame)
    assert list(df.columns)[:3] == ["num_trans", "nwords", "span"]
    assert len(df) == 12


# ---------------------------------------------------------------------------
# Feature
# ---------------------------------------------------------------------------

def test_feature_raw_and_transform():
    raw = Feature("nwords")
    assert raw.value({"nwords": 7}) == 7.0
    sq = Feature("sqrt_nc", lambda r: r["n_col"] ** 0.5)
    assert sq.value({"n_col": 256}) == pytest.approx(16.0)


def test_feature_coerce_forms():
    assert Feature.coerce("x").name == "x"
    f = Feature.coerce(("y", lambda r: 2 * r["x"]))
    assert f.name == "y" and f.value({"x": 3}) == 6.0
    g = Feature("z")
    assert Feature.coerce(g) is g
    with pytest.raises(TypeError):
        Feature.coerce(123)


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
    assert m.predict({"num_trans": 3, "nwords": 300}) == pytest.approx(315.0)


def test_linear_with_intercept():
    db = CalibDatabase(["x", "y"])
    for x in range(10):
        db.add_datapoint({"x": x, "y": 7 + 3 * x})
    m = LinCalibModel(["x"], "y", fit_intercept=True).fit(db)
    assert m.coeffs["x"] == pytest.approx(3.0)
    assert m.coeffs["intercept"] == pytest.approx(7.0)


def test_predict_requires_fit():
    m = LinCalibModel(["x"], "y")
    with pytest.raises(RuntimeError):
        m.predict({"x": 1})


def test_basis_function_generality_sqrt():
    # A genuinely concave target the pure-linear basis cannot fit but a sqrt basis can.
    db = CalibDatabase(["n_col", "t"])
    for nc in (64, 256, 1024, 4096):
        db.add_datapoint({"n_col": nc, "t": 10.0 + 3.0 * nc ** 0.5})
    lin = LinCalibModel(["n_col"], "t").fit(db)
    sqrtm = LinCalibModel(
        [Feature("sqrt_nc", lambda r: r["n_col"] ** 0.5)], "t", fit_intercept=True
    ).fit(db)
    assert sqrtm.score(db) == pytest.approx(1.0, abs=1e-9)
    assert sqrtm.score(db) > lin.score(db)   # the right basis wins


# ---------------------------------------------------------------------------
# Metrics / holdout
# ---------------------------------------------------------------------------

def test_rel_errors_skip_zero_actual():
    db = CalibDatabase(["x", "y"])
    db.add_datapoint({"x": 0, "y": 0})
    db.add_datapoint({"x": 1, "y": 10})
    db.add_datapoint({"x": 2, "y": 20})
    m = LinCalibModel(["x"], "y", fit_intercept=False).fit(db)
    errs = m.rel_errors(db)
    assert len(errs) == 2          # the y==0 row is skipped
    assert max(errs) < 1e-9


def test_holdout_report_structure():
    db = _grid_db()
    train = db.filter(lambda r: not (r["num_trans"] == 2 and r["nwords"] == 512))
    test = db.filter(lambda r: r["num_trans"] == 2 and r["nwords"] == 512)
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
        m.predict({"x": 1})   # not fitted


def test_interp_model_lookup_and_saturation():
    # A saturating curve (like the per-row pipeline depth g(n_col)).
    db = CalibDatabase(["n_col", "g"])
    for nc, g in [(64, 70.0), (256, 260.0), (1024, 268.0)]:
        db.add_datapoint({"n_col": nc, "g": g})
    m = InterpCalibModel([Feature("n_col")], "g").fit(db)
    # exact at samples
    assert m.predict({"n_col": 256}) == pytest.approx(260.0)
    # linear interpolation between samples (untrained n_col)
    assert m.predict({"n_col": 160}) == pytest.approx(70.0 + (260.0 - 70.0) * (160 - 64) / (256 - 64))
    # flat extrapolation beyond the range (the saturation)
    assert m.predict({"n_col": 4096}) == pytest.approx(268.0)
    assert m.predict({"n_col": 1}) == pytest.approx(70.0)


def test_interp_model_averages_duplicates():
    # g measured at several n_row for the same n_col -> averaged.
    db = CalibDatabase(["n_col", "g"])
    for g in (69.0, 70.0, 71.0):
        db.add_datapoint({"n_col": 256, "g": g})
    db.add_datapoint({"n_col": 1024, "g": 268.0})
    m = InterpCalibModel([Feature("n_col")], "g").fit(db)
    assert m.predict({"n_col": 256}) == pytest.approx(70.0)


def test_interp_model_roundtrip_samples():
    db = CalibDatabase(["x", "y"])
    for x, y in [(1, 10.0), (2, 12.0), (4, 13.0)]:
        db.add_datapoint({"x": x, "y": y})
    m = InterpCalibModel([Feature("x")], "y").fit(db)
    s = m.samples
    assert s["feature"] == "x" and s["x"] == [1.0, 2.0, 4.0] and s["y"] == [10.0, 12.0, 13.0]
    m2 = InterpCalibModel.from_samples("x", s["x"], s["y"], "y")
    assert m2.predict({"x": 3}) == pytest.approx(m.predict({"x": 3}))


def test_interp_model_rejects_multifeature():
    with pytest.raises(ValueError):
        InterpCalibModel(["a", "b"], "y")


def test_plot_smoke():
    pytest.importorskip("matplotlib")
    import matplotlib
    matplotlib.use("Agg")
    db = _grid_db()
    m = LinCalibModel(["num_trans", "nwords"], "span", fit_intercept=False).fit(db)
    ax = m.plot(db, x_name="nwords")
    assert ax is not None
