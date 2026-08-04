"""tests/calib/test_timing_model.py — the residual-fitting orchestration.

The model machinery (LinCalibModel) is covered in test_calib.py; this file is about the layer
TimingModel adds: independent rtl/pysim collection, per-firing validity, the merged residual frame
(with the current-prediction correction), cycle/time conversion, coverage, and the lifecycle.
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


class _Clk:
    def __init__(self, period):
        self.period = period


@pytest.fixture
def tm(tmp_path):
    return StreamTimingModel(component="mem_w_stream_framed_done_task", calib_dir=tmp_path / "cal")


def _seed_two_sizes(m, component="c"):
    """Collect two sizes whose true residual is 2*num_trans (nwords contributes 0)."""
    for nw in (128, 512):
        nt = nw // 16
        m.collect_rtl(_rtl_events(component=component, nwords=nw, span=1000 + nw + 2 * nt),
                      run_id=f"n{nw}")
        m.collect_pysim(_pysim_firings(nwords=nw, span=1000 + nw, current_dly=0.0), run_id=f"n{nw}")


class TestConstruction:
    def test_stream_defaults_the_basis(self, tmp_path):
        assert StreamTimingModel(component="c", calib_dir=tmp_path).features == ["nwords", "num_trans"]

    def test_base_requires_features(self, tmp_path):
        with pytest.raises(ValueError, match="features"):
            TimingModel(component="c", calib_dir=tmp_path)

    def test_unfitted_predicts_the_seed(self, tmp_path):
        m = StreamTimingModel(component="c", calib_dir=tmp_path)
        assert m.predict_feat({"nwords": 128, "num_trans": 8}) == [0.0]

    def test_nonzero_seed_is_used_before_fit(self, tmp_path):
        m = StreamTimingModel(component="c", calib_dir=tmp_path,
                              seed={"nwords": 0.0, "num_trans": 2.0, "intercept": 22.0})
        assert m.predict_feat({"nwords": 128, "num_trans": 8}) == [22.0 + 2.0 * 8]


class TestResidualAndFit:
    def test_residual_subtracts_pysim_and_adds_current_dly(self, tm):
        """The crux: residual = rtl - pysim + current_dly, so the fit is self-correcting."""
        tm.collect_rtl(_rtl_events(span=183), run_id="n128")
        tm.collect_pysim(_pysim_firings(span=150, current_dly=5.0), run_id="n128")
        df = tm.gen_data_frame()
        assert len(df) == 1
        assert df["residual"].iloc[0] == pytest.approx(183 - 150 + 5)

    def test_blocked_firings_are_dropped(self, tm):
        ev = _rtl_events(span=183, n_firings=16)
        for f in ev["firings"][1:]:            # firing 0 clean, rest contended
            f["blocked"], f["span"] = 30, 213
        tm.collect_rtl(ev, run_id="n128")
        tm.collect_pysim(_pysim_firings(span=150), run_id="n128")
        # median over the ONE valid RTL firing = 183, not the contended 213.
        assert tm.gen_data_frame()["residual"].iloc[0] == pytest.approx(183 - 150)

    def test_fit_recovers_a_linear_law_across_sizes(self, tmp_path):
        m = StreamTimingModel(component="c", calib_dir=tmp_path)
        _seed_two_sizes(m)
        m.fit()
        assert m.predict_feat({"nwords": 256, "num_trans": 16})[0] == pytest.approx(2 * 16, abs=1e-6)

    def test_fit_writes_params_json(self, tm):
        tm.collect_rtl(_rtl_events(), run_id="n128")
        tm.collect_pysim(_pysim_firings(), run_id="n128")
        tm.collect_rtl(_rtl_events(nwords=512, span=615), run_id="n512")
        tm.collect_pysim(_pysim_firings(nwords=512, span=531), run_id="n512")
        tm.fit()
        params = json.loads((tm.calib_dir / "params.json").read_text())
        assert "intercept" in params and set(tm.features) <= set(params)

    def test_deployed_model_reloads_from_params(self, tmp_path):
        a = StreamTimingModel(component="c", calib_dir=tmp_path)
        _seed_two_sizes(a)
        a.fit()
        want = a.predict_feat({"nwords": 256, "num_trans": 16})[0]
        b = StreamTimingModel(component="c", calib_dir=tmp_path)   # fresh, only reads params.json
        assert b.predict_feat({"nwords": 256, "num_trans": 16})[0] == pytest.approx(want)

    def test_empty_join_fit_raises(self, tm):
        tm.collect_rtl(_rtl_events(), run_id="n128")   # RTL only, no pysim
        with pytest.raises(RuntimeError, match="no residual datapoints"):
            tm.fit()

    def test_predict_never_negative(self, tmp_path):
        m = StreamTimingModel(component="c", calib_dir=tmp_path,
                              seed={"nwords": 0.0, "num_trans": 0.0, "intercept": -5.0})
        assert m.predict_feat({"nwords": 0, "num_trans": 0}) == [0.0]

    def test_corpus_csv_is_written_and_inspectable(self, tm):
        tm.collect_rtl(_rtl_events(span=183), run_id="n128")
        tm.collect_pysim(_pysim_firings(span=147, current_dly=0.0), run_id="n128")
        tm.gen_data_frame()
        import pandas as pd
        corpus = pd.read_csv(tm.corpus_path)
        assert list(corpus.columns) == ["nwords", "num_trans", "span_rtl", "span_pysim",
                                        "current_dly", "residual"]
        assert corpus["residual"].iloc[0] == pytest.approx(36)


class TestIndependentCadenceAndCoverage:
    def test_rtl_and_pysim_collect_into_separate_trees(self, tm):
        tm.collect_rtl(_rtl_events(), run_id="n128")
        tm.collect_pysim(_pysim_firings(), run_id="whenever")   # different run id, fine
        assert (tm.calib_dir / "rtl" / "n128" / "firings.csv").exists()
        assert (tm.calib_dir / "pysim" / "whenever" / "firings.csv").exists()

    def test_join_is_on_features_not_run_id(self, tm):
        """RTL and pysim at n=128 correspond even under different run ids."""
        tm.collect_rtl(_rtl_events(nwords=128, span=183), run_id="rtl_sweep_a")
        tm.collect_pysim(_pysim_firings(nwords=128, span=147), run_id="pysim_nightly_42")
        df = tm.gen_data_frame()
        assert len(df) == 1 and df["residual"].iloc[0] == pytest.approx(36)

    def test_coverage_reports_unmatched_points(self, tm):
        """pysim ran more sizes than RTL — the extra points are reported, not silently dropped."""
        tm.collect_rtl(_rtl_events(nwords=128, span=183), run_id="a")
        tm.collect_pysim(_pysim_firings(nwords=128, span=147), run_id="a")
        tm.collect_pysim(_pysim_firings(nwords=256, span=291), run_id="b")   # no RTL counterpart
        df = tm.gen_data_frame()
        assert len(df) == 1                                # only the matched point fits
        assert tm.coverage["matched"] == [(128.0, 8.0)]
        assert tm.coverage["pysim_only"] == [(256.0, 16.0)]
        assert tm.coverage["rtl_only"] == []

    def test_rerunning_a_scenario_overwrites(self, tm):
        tm.collect_rtl(_rtl_events(span=183), run_id="n128")
        tm.collect_rtl(_rtl_events(span=999), run_id="n128")   # same run_id -> replace
        tm.collect_pysim(_pysim_firings(), run_id="n128")
        assert tm.gen_data_frame()["residual"].iloc[0] == pytest.approx(999 - 147)


class TestCycleTimeConversion:
    def test_pysim_time_is_converted_to_cycles_on_collect(self, tmp_path):
        """clk set: pysim spans arrive in TIME and are divided to cycles; residual is in cycles."""
        clk = _Clk(period=10.0)                # 10 ns/cycle
        m = StreamTimingModel(component="c", calib_dir=tmp_path, clk=clk)
        m.collect_rtl(_rtl_events(component="c", nwords=128, span=183), run_id="n128")  # cycles
        m.collect_pysim([{"nwords": 128, "num_trans": 8, "span": 1470.0, "current_dly": 0.0}],
                        run_id="n128")          # 1470 ns = 147 cycles
        df = m.gen_data_frame()
        assert df["span_pysim"].iloc[0] == pytest.approx(147)
        assert df["residual"].iloc[0] == pytest.approx(183 - 147)

    def test_predict_returns_time_when_clk_set(self, tmp_path):
        clk = _Clk(period=10.0)
        m = StreamTimingModel(component="c", calib_dir=tmp_path, clk=clk,
                              seed={"nwords": 0.0, "num_trans": 0.0, "intercept": 5.0})
        # 5 cycles * 10 ns = 50 ns
        assert m.predict_feat({"nwords": 128, "num_trans": 8}) == [50.0]

    def test_params_are_clock_independent(self, tmp_path):
        """The fitted state_dict is cycles — the SAME params drive different clocks, only the
        boundary conversion differs.  This is the payoff of cycles-internal."""
        a = StreamTimingModel(component="c", calib_dir=tmp_path / "a", clk=_Clk(10.0))
        for nw in (128, 512):
            a.collect_rtl(_rtl_events(component="c", nwords=nw, span=1000 + nw + 2 * (nw // 16)),
                          run_id=f"n{nw}")
            a.collect_pysim([{"nwords": nw, "num_trans": nw // 16,
                              "span": (1000 + nw) * 10.0, "current_dly": 0.0}], run_id=f"n{nw}")
        a.fit()
        params = json.loads((a.calib_dir / "params.json").read_text())

        # Same params, a slower clock: predict scales with the period, params do not.
        b = StreamTimingModel(component="c", calib_dir=tmp_path / "a", clk=_Clk(20.0))
        assert b.predict_feat({"nwords": 256, "num_trans": 16})[0] == pytest.approx(2 * 16 * 20.0)
        assert json.loads((b.calib_dir / "params.json").read_text()) == params


class TestValidityHook:
    def test_is_record_valid_is_overridable(self, tmp_path):
        """A model can supply its own validity test (e.g. an AXI stall scan)."""
        class OnlyEven(StreamTimingModel):
            def is_record_valid(self, firing, run_dir):
                return int(firing["index"]) % 2 == 0     # keep even firings only

        m = OnlyEven(component="c", calib_dir=tmp_path)
        ev = _rtl_events(component="c", n_firings=4, span=100)
        for i, f in enumerate(ev["firings"]):
            f["span"] = 100 + i                            # spans 100,101,102,103
        m.collect_rtl(ev, run_id="n128")
        m.collect_pysim(_pysim_firings(nwords=128, span=0.0), run_id="n128")
        # median of even-index spans (100, 102) = 101.
        assert m.gen_data_frame()["span_rtl"].iloc[0] == pytest.approx(101)


class TestReset:
    def test_reset_wipes_the_corpus_by_default(self, tm):
        tm.collect_rtl(_rtl_events(), run_id="n128")
        tm.collect_pysim(_pysim_firings(), run_id="n128")
        tm.gen_data_frame()
        tm.reset()
        assert not (tm.calib_dir / "rtl").exists()
        assert not (tm.calib_dir / "pysim").exists()
        assert not tm.corpus_path.exists()

    def test_reset_default_keeps_params(self, tmp_path):
        m = StreamTimingModel(component="c", calib_dir=tmp_path)
        _seed_two_sizes(m)
        m.fit()
        m.reset()
        assert (m.calib_dir / "params.json").exists()

    def test_reset_params_falls_back_to_seed(self, tmp_path):
        m = StreamTimingModel(component="c", calib_dir=tmp_path)
        _seed_two_sizes(m)
        m.fit()
        assert m.predict_feat({"nwords": 256, "num_trans": 16})[0] == pytest.approx(32)
        m.reset(corpus=False, params=True)
        assert not (m.calib_dir / "params.json").exists()
        assert m.predict_feat({"nwords": 256, "num_trans": 16})[0] == 0.0


class TestMultiTargetGuard:
    def test_construction_is_allowed(self, tmp_path):
        assert StreamTimingModel(component="c", calib_dir=tmp_path, num_targets=3).num_targets == 3

    def test_every_operation_raises(self, tmp_path):
        m = StreamTimingModel(component="c", calib_dir=tmp_path, num_targets=3)
        with pytest.raises(NotImplementedError, match="per-stage"):
            m.predict_feat({"nwords": 128, "num_trans": 8})
        with pytest.raises(NotImplementedError):
            m.collect_rtl(_rtl_events(), run_id="x")
        with pytest.raises(NotImplementedError):
            m.gen_data_frame()
