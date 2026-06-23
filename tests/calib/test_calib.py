"""Unit tests for waveflow.calib — the bare-bones calibration infrastructure.

Covers the corpus (CalibDataFrame, backed by a pandas DataFrame), the
LinCalibModel fit/predict/score/holdout behaviour (including the through-origin
``span = setup·num_trans + per_word·nwords`` form FIR uses and the basis
generality via caller-side derived columns), and the InterpCalibModel lookup.
"""
from __future__ import annotations

import pandas as pd
import pytest

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
    assert m.predict({"num_trans": 3, "nwords": 300}) == pytest.approx(315.0)


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
        m.predict({"x": 1})


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
        m.predict({"x": 1})   # not fitted


def test_interp_model_lookup_and_saturation():
    # A saturating curve (like the per-row pipeline depth row_depth(n_col)).
    db = CalibDataFrame(["n_col", "row_depth"])
    for nc, rd in [(64, 70.0), (256, 260.0), (1024, 268.0)]:
        db.add_datapoint({"n_col": nc, "row_depth": rd})
    m = InterpCalibModel(["n_col"], "row_depth").fit(db)
    # exact at samples
    assert m.predict({"n_col": 256}) == pytest.approx(260.0)
    # linear interpolation between samples (untrained n_col)
    assert m.predict({"n_col": 160}) == pytest.approx(70.0 + (260.0 - 70.0) * (160 - 64) / (256 - 64))
    # flat extrapolation beyond the range (the saturation)
    assert m.predict({"n_col": 4096}) == pytest.approx(268.0)
    assert m.predict({"n_col": 1}) == pytest.approx(70.0)


def test_interp_model_averages_duplicates():
    # row_depth measured at several n_row for the same n_col -> averaged.
    db = CalibDataFrame(["n_col", "row_depth"])
    for rd in (69.0, 70.0, 71.0):
        db.add_datapoint({"n_col": 256, "row_depth": rd})
    db.add_datapoint({"n_col": 1024, "row_depth": 268.0})
    m = InterpCalibModel(["n_col"], "row_depth").fit(db)
    assert m.predict({"n_col": 256}) == pytest.approx(70.0)


def test_interp_model_roundtrip_samples():
    db = CalibDataFrame(["x", "y"])
    for x, y in [(1, 10.0), (2, 12.0), (4, 13.0)]:
        db.add_datapoint({"x": x, "y": y})
    m = InterpCalibModel(["x"], "y").fit(db)
    s = m.samples
    assert s["feature"] == "x" and s["x"] == [1.0, 2.0, 4.0] and s["y"] == [10.0, 12.0, 13.0]
    m2 = InterpCalibModel.from_samples("x", s["x"], s["y"], "y")
    assert m2.predict({"x": 3}) == pytest.approx(m.predict({"x": 3}))


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
