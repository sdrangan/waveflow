"""tests/calib/test_timing_model.py — the residual-fitting orchestration.

The model machinery (LinCalibModel) is covered in test_calib.py; this file is about the layer
TimingModel adds: collecting per-run firings, filtering on `blocked`, joining RTL vs pysim into the
*current-prediction-corrected* residual, and the lifecycle (seed fallback, reset, num_targets guard).
"""
from __future__ import annotations

import json
import math

import pytest

from waveflow.calib.timing_model import StreamTimingModel, TimingModel


def _rtl_events(component="mem_w_stream_framed_done_task", *, nwords=128, n_firings=16,
                span=183, blocked=0, max_burst=16):
    """An ExtractBurstsStep-shaped events dict: n_firings of one component, all identical."""
    num_trans = math.ceil(nwords / max_burst)
    return {"version": 1, "top": "mem_copy", "max_burst_len": max_burst,
            "firings": [{"component": component, "inst": f"{component}_U0", "index": i,
                         "start": 0, "end": span - 1, "span": span,
                         "nwords": nwords, "num_trans": num_trans, "blocked": blocked}
                        for i in range(n_firings)]}


def _pysim_firings(*, nwords=128, n=16, span=147, current_dly=0.0, max_burst=16):
    num_trans = math.ceil(nwords / max_burst)
    return [{"nwords": nwords, "num_trans": num_trans, "span": span, "current_dly": current_dly}
            for _ in range(n)]


@pytest.fixture
def tm(tmp_path):
    return StreamTimingModel(component="mem_w_stream_framed_done_task", calib_dir=tmp_path / "cal")


class TestConstruction:
    def test_stream_defaults_the_basis(self, tmp_path):
        m = StreamTimingModel(component="c", calib_dir=tmp_path)
        assert m.features == ["nwords", "num_trans"]

    def test_base_requires_features(self, tmp_path):
        with pytest.raises(ValueError, match="features"):
            TimingModel(component="c", calib_dir=tmp_path)

    def test_unfitted_predicts_the_seed(self, tmp_path):
        """A fresh model with the default (zero) seed adds no delay — a component with a TimingModel
        but no calibration still simulates."""
        m = StreamTimingModel(component="c", calib_dir=tmp_path)
        assert m.predict({"nwords": 128, "num_trans": 8}) == [0.0]

    def test_nonzero_seed_is_used_before_fit(self, tmp_path):
        m = StreamTimingModel(component="c", calib_dir=tmp_path,
                              seed={"nwords": 0.0, "num_trans": 2.0, "intercept": 22.0})
        assert m.predict({"nwords": 128, "num_trans": 8}) == [22.0 + 2.0 * 8]


class TestCollectAndFit:
    def test_residual_subtracts_pysim_and_adds_current_dly(self, tm):
        """The crux: target = rtl - pysim + current_dly.  With current_dly recorded, the fit is
        self-correcting from any prior model state."""
        # RTL 183, pysim 150 measured WHILE the model was already adding 5 -> true residual 38.
        tm.collect_rtl(_rtl_events(span=183), run_id="n128")
        tm.collect_pysim(_pysim_firings(span=150, current_dly=5.0), run_id="n128")
        corpus = tm.build_corpus()
        assert len(corpus) == 1
        assert corpus.df[tm._model.target].iloc[0] == pytest.approx(183 - 150 + 5)

    def test_blocked_firings_are_dropped(self, tm):
        """Only blocked==0 firings are the component's own cost."""
        ev = _rtl_events(span=183, n_firings=16)
        for f in ev["firings"][1:]:            # firing 0 clean, rest contended
            f["blocked"] = 30
            f["span"] = 213
        tm.collect_rtl(ev, run_id="n128")
        tm.collect_pysim(_pysim_firings(span=150), run_id="n128")
        corpus = tm.build_corpus()
        # median RTL span over the ONE valid firing = 183, not the contended 213.
        assert corpus.df[tm._model.target].iloc[0] == pytest.approx(183 - 150 + 0)

    def test_fit_recovers_a_linear_law_across_sizes(self, tmp_path):
        """Two sizes, residual = 22 + 2*num_trans (nwords contributes 0): the fit must recover it,
        which a single size could not."""
        m = StreamTimingModel(component="c", calib_dir=tmp_path)
        for nw in (128, 512):
            nt = nw // 16
            m.collect_rtl(_rtl_events(component="c", nwords=nw, span=1000 + nw + 2 * nt),
                          run_id=f"n{nw}")
            m.collect_pysim(_pysim_firings(nwords=nw, span=1000 + nw, current_dly=0.0),
                            run_id=f"n{nw}")
        m.fit()
        # residual(nw) = 2*num_trans -> predict at an UNSEEN size extrapolates.
        pred = m.predict({"nwords": 256, "num_trans": 16})[0]
        assert pred == pytest.approx(2 * 16, abs=1e-6)

    def test_fit_writes_params_json(self, tm):
        tm.collect_rtl(_rtl_events(), run_id="n128")
        tm.collect_pysim(_pysim_firings(), run_id="n128")
        tm.fit()
        params = json.loads((tm.calib_dir / "params.json").read_text())
        assert "intercept" in params and set(tm.features) <= set(params)

    def test_fitted_model_reloads_from_params(self, tmp_path):
        """A deployed model predicts from params.json alone — no corpus, no sklearn refit."""
        a = StreamTimingModel(component="c", calib_dir=tmp_path)
        for nw in (128, 512):
            a.collect_rtl(_rtl_events(component="c", nwords=nw, span=1000 + nw + 2 * (nw // 16)),
                          run_id=f"n{nw}")
            a.collect_pysim(_pysim_firings(nwords=nw, span=1000 + nw), run_id=f"n{nw}")
        a.fit()
        want = a.predict({"nwords": 256, "num_trans": 16})[0]

        b = StreamTimingModel(component="c", calib_dir=tmp_path)   # fresh, only reads params.json
        assert b.predict({"nwords": 256, "num_trans": 16})[0] == pytest.approx(want)

    def test_empty_corpus_fit_raises(self, tm):
        """A fit with nothing joined must fail, not silently leave a seed model looking fitted."""
        tm.collect_rtl(_rtl_events(), run_id="n128")   # RTL only, no pysim
        with pytest.raises(RuntimeError, match="no residual datapoints"):
            tm.fit()

    def test_predict_never_negative(self, tmp_path):
        """A residual model that extrapolates below zero must clamp — a negative timeout is meaningless."""
        m = StreamTimingModel(component="c", calib_dir=tmp_path,
                              seed={"nwords": 0.0, "num_trans": 0.0, "intercept": -5.0})
        assert m.predict({"nwords": 0, "num_trans": 0}) == [0.0]


class TestPerRunStorage:
    def test_each_run_is_its_own_folder(self, tm):
        tm.collect_rtl(_rtl_events(), run_id="n128")
        tm.collect_rtl(_rtl_events(nwords=256, span=327), run_id="n256")
        assert (tm.runs_dir / "n128" / "rtl_firings.csv").exists()
        assert (tm.runs_dir / "n256" / "rtl_firings.csv").exists()

    def test_rerunning_a_scenario_overwrites_not_duplicates(self, tm):
        tm.collect_rtl(_rtl_events(span=183), run_id="n128")
        tm.collect_rtl(_rtl_events(span=999), run_id="n128")   # same run_id
        tm.collect_pysim(_pysim_firings(), run_id="n128")
        corpus = tm.build_corpus()
        assert corpus.df[tm._model.target].iloc[0] == pytest.approx(999 - 147 + 0)

    def test_fit_globs_all_runs(self, tmp_path):
        m = StreamTimingModel(component="c", calib_dir=tmp_path)
        m.collect_rtl(_rtl_events(component="c", nwords=128, span=1128 + 2 * 8), run_id="a")
        m.collect_pysim(_pysim_firings(nwords=128, span=1128), run_id="a")
        m.collect_rtl(_rtl_events(component="c", nwords=512, span=1512 + 2 * 32), run_id="b")
        m.collect_pysim(_pysim_firings(nwords=512, span=1512), run_id="b")
        assert len(m.build_corpus()) == 2      # both runs joined


class TestReset:
    def test_reset_wipes_runs_by_default(self, tm):
        tm.collect_rtl(_rtl_events(), run_id="n128")
        assert tm.runs_dir.exists()
        tm.reset()
        assert not tm.runs_dir.exists()

    def test_reset_params_falls_back_to_seed(self, tmp_path):
        m = StreamTimingModel(component="c", calib_dir=tmp_path)
        for nw in (128, 512):
            m.collect_rtl(_rtl_events(component="c", nwords=nw, span=1000 + nw + 2 * (nw // 16)),
                          run_id=f"n{nw}")
            m.collect_pysim(_pysim_firings(nwords=nw, span=1000 + nw), run_id=f"n{nw}")
        m.fit()
        assert m.predict({"nwords": 256, "num_trans": 16})[0] == pytest.approx(32)

        m.reset(runs=False, params=True)
        assert not (m.calib_dir / "params.json").exists()
        assert m.predict({"nwords": 256, "num_trans": 16})[0] == 0.0   # back to the zero seed

    def test_reset_default_keeps_params(self, tmp_path):
        m = StreamTimingModel(component="c", calib_dir=tmp_path)
        for nw in (128, 512):
            m.collect_rtl(_rtl_events(component="c", nwords=nw, span=1000 + nw + 2 * (nw // 16)),
                          run_id=f"n{nw}")
            m.collect_pysim(_pysim_firings(nwords=nw, span=1000 + nw), run_id=f"n{nw}")
        m.fit()
        m.reset()                              # runs only
        assert (m.calib_dir / "params.json").exists()


class TestMultiTargetGuard:
    def test_construction_is_allowed(self, tmp_path):
        """The API is carried — you can declare num_targets > 1..."""
        m = StreamTimingModel(component="c", calib_dir=tmp_path, num_targets=3)
        assert m.num_targets == 3

    def test_every_operation_raises(self, tmp_path):
        """...but nothing works until per-stage measurement (Tier-2) exists."""
        m = StreamTimingModel(component="c", calib_dir=tmp_path, num_targets=3)
        with pytest.raises(NotImplementedError, match="per-stage"):
            m.predict({"nwords": 128, "num_trans": 8})
        with pytest.raises(NotImplementedError):
            m.collect_rtl(_rtl_events(), run_id="x")
        with pytest.raises(NotImplementedError):
            m.fit()
