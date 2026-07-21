"""tests/calib/test_bus_model.py — platform-scoped bus-transfer calibration.

BusCalib fits a per-direction span(num_trans, nwords) model from the memory ports and stores it in a
platform directory, so every accelerator on that platform reuses it without recalibrating.  This
checks fit -> persist -> load -> a configured BusTiming that predicts the span; and the
load-or-default degrade when a platform has no fit yet.
"""
from __future__ import annotations

import json

import pytest

from waveflow.calib.bus_model import BUS_MODEL_FILE, BusCalib


def _points(law):
    """{num_trans, nwords, span} datapoints from a span law, at a few sizes."""
    return [{"num_trans": nt, "nwords": nw, "span": law(nt, nw)}
            for nw, nt in [(128, 8), (256, 16), (512, 32), (64, 4)]]


class TestFitPersistLoad:
    def test_fit_writes_the_platform_model(self, tmp_path):
        bc = BusCalib(platform_dir=tmp_path)
        bc.fit(read_points=_points(lambda nt, nw: nw + 2 * (nt - 1)),
               write_points=_points(lambda nt, nw: nw + 2 * (nt - 1)))
        assert (tmp_path / BUS_MODEL_FILE).exists()
        data = json.loads((tmp_path / BUS_MODEL_FILE).read_text())
        assert set(data["models"]) == {"read", "write"}
        assert data["basis"] == ["num_trans", "nwords"]

    def test_loaded_bus_timing_predicts_the_span(self, tmp_path):
        """The fitted model reproduces the law: span = nwords + 2*(num_trans-1) cycles."""
        BusCalib(platform_dir=tmp_path, clk_freq=100e6).fit(
            None, write_points=_points(lambda nt, nw: nw + 2 * (nt - 1)))
        bt = BusCalib(platform_dir=tmp_path, clk_freq=100e6).bus_timing()
        # write_span_secs -> cycles/clk_freq; 128 words in 8 bursts = 128 + 14 = 142 cycles.
        assert bt.write_span_secs(num_trans=8, nwords=128) == pytest.approx(142 / 100e6, rel=1e-6)
        assert bt.read_span_secs(num_trans=8, nwords=128) is None   # read never fit -> no model

    def test_a_write_only_platform_fits_only_write(self, tmp_path):
        BusCalib(platform_dir=tmp_path).fit(None, _points(lambda nt, nw: nw))
        data = json.loads((tmp_path / BUS_MODEL_FILE).read_text())
        assert set(data["models"]) == {"write"}


class TestLoadOrDefault:
    def test_uncalibrated_platform_degrades_to_the_word_bw_fallback(self, tmp_path):
        """No mm_bus.json -> an unconfigured BusTiming (both directions None), so a slice falls back
        to the plain per-word span rather than crashing."""
        bt = BusCalib(platform_dir=tmp_path / "never_calibrated").bus_timing()
        assert bt.read is None and bt.write is None
        assert bt.read_span_secs(num_trans=8, nwords=128) is None

    def test_reuse_across_projects_needs_no_refit(self, tmp_path):
        """The whole point: fit once, then a *different* BusCalib pointed at the same platform dir
        loads the model without any datapoints."""
        BusCalib(platform_dir=tmp_path).fit(None, _points(lambda nt, nw: nw + 2 * (nt - 1)))
        # A fresh instance (as a new project would build) -- no fit, just load.
        reused = BusCalib(platform_dir=tmp_path).bus_timing()
        assert reused.write_span_secs(num_trans=32, nwords=512) == pytest.approx(
            (512 + 62) / 100e6, rel=1e-6)


class TestCorpusAccumulation:
    def test_add_run_then_fit_from_corpus(self, tmp_path):
        """A sweep: add_run per size (distinct point files), then a single fit() from the corpus."""
        bc = BusCalib(platform_dir=tmp_path, clk_freq=100e6)
        bc.add_run("n128", write={"num_trans": 8, "nwords": 128, "span": 142})
        bc.add_run("n512", write={"num_trans": 32, "nwords": 512, "span": 574})
        assert (tmp_path / "points" / "n128.json").exists()

        bc.fit()                                     # no args -> read the corpus
        bt = BusCalib(platform_dir=tmp_path, clk_freq=100e6).bus_timing()
        assert bt.write_span_secs(8, 128) == pytest.approx(142 / 100e6, rel=1e-6)
        assert bt.write_span_secs(32, 512) == pytest.approx(574 / 100e6, rel=1e-6)

    def test_rerunning_a_size_overwrites_its_point(self, tmp_path):
        bc = BusCalib(platform_dir=tmp_path)
        bc.add_run("n128", write={"num_trans": 8, "nwords": 128, "span": 999})
        bc.add_run("n128", write={"num_trans": 8, "nwords": 128, "span": 142})   # same run_id
        r, w = bc._corpus()
        assert len(w) == 1 and w[0]["span"] == 142

    def test_the_committed_corpus_is_distilled_points_not_traces(self, tmp_path):
        """What gets checked in: the small distilled point (KB), which re-fits without a toolchain.

        The raw VCD is gitignored; the corpus is these {num_trans, nwords, span} rows."""
        import json
        bc = BusCalib(platform_dir=tmp_path)
        bc.add_run("n128", read={"num_trans": 8, "nwords": 128, "span": 135})
        rec = json.loads((tmp_path / "points" / "n128.json").read_text())
        assert set(rec["read"]) == {"num_trans", "nwords", "span"}
