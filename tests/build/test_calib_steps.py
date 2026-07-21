"""tests/build/test_calib_steps.py — the collect + fit DAG steps.

The TimingModel engine and the FreeRunComp instrumentation are unit-tested elsewhere; this checks the
two build steps wire them: CollectTimingStep walks the design's attached models and appends a run's
rtl+pysim firings; FitTimingStep fits each from the accumulated corpus (skipping, not failing, the
ones that do not yet join).  A synthetic single-leaf design stands in for a real example.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from waveflow.build.build import BuildConfig
from waveflow.build.calib_steps import CollectTimingStep, FitTimingStep
from waveflow.calib.timing_model import StreamTimingModel
from waveflow.hw.hw_freerun import FreeRunComp
from waveflow.simulation.simulation import Simulation


@dataclass
class _Leaf(FreeRunComp):
    """A leaf whose one firing takes `base + nwords` time, so its pysim span tracks the feature."""
    nwords: int = 128
    base: float = 1000.0
    _count: int = field(default=0)

    def run_iter(self):
        if self._count >= 1:
            yield self.timeout(10_000)
            return
        self._count += 1
        yield self.timeout(self.base + self.nwords)
        yield self.timeout(self.timed_delay(
            {"nwords": self.nwords, "num_trans": math.ceil(self.nwords / 16)}))


def _run_leaf(calib_dir, nwords):
    """Build a calibrated leaf and run one firing in pysim (populating firing_records)."""
    sim = Simulation()
    comp = _Leaf(name="c", sim=sim, nwords=nwords)
    comp.add_timing_model(StreamTimingModel(component="c", calib_dir=calib_dir))
    sim.env.process(comp._run_iter_forever())
    sim.env.run(until=nwords + 2000)
    return comp


def _events(nwords, span):
    """A synthetic ExtractBurstsStep events dict for component 'c' at one size."""
    return {"top": "t", "max_burst_len": 16,
            "firings": [{"component": "c", "index": 0, "nwords": nwords,
                         "num_trans": math.ceil(nwords / 16), "span": span, "blocked": 0}]}


def _write_events(tmp_path, nwords, span):
    p = tmp_path / f"events_{nwords}.json"
    p.write_text(json.dumps(_events(nwords, span)))
    return p


class TestCollect:
    def test_collect_appends_both_sides(self, tmp_path):
        calib = tmp_path / "cal"
        step = CollectTimingStep(name="collect", run_id="n128",
                                 run_pysim=lambda: _run_leaf(calib, 128))
        ev = _write_events(tmp_path, 128, span=1183)
        out = step.run(BuildConfig(root_dir=str(tmp_path)), timing_events=ev)

        assert (calib / "rtl" / "n128" / "firings.csv").exists()
        assert (calib / "pysim" / "n128" / "firings.csv").exists()
        assert out["timing_collected"].exists()
        report = json.loads(out["timing_collected"].read_text())
        assert report["models"][0] == {"component": "c", "pysim_firings": 1}

    def test_no_models_is_an_error(self, tmp_path):
        """A design built without calib_dir has no models — the step should say so, not no-op."""
        def bare():
            sim = Simulation()
            return _Leaf(name="c", sim=sim)     # no add_timing_model

        step = CollectTimingStep(name="collect", run_id="x", run_pysim=bare)
        ev = _write_events(tmp_path, 128, 1183)
        with pytest.raises(RuntimeError, match="no attached TimingModel"):
            step.run(BuildConfig(root_dir=str(tmp_path)), timing_events=ev)


class TestFit:
    def test_fit_after_two_points_writes_params(self, tmp_path):
        calib = tmp_path / "cal"
        # Two sweep points; RTL span = pysim span + (10 + 2*num_trans) residual.
        for nw in (128, 512):
            nt = math.ceil(nw / 16)
            CollectTimingStep(name="c", run_id=f"n{nw}",
                              run_pysim=lambda nw=nw: _run_leaf(calib, nw)).run(
                BuildConfig(root_dir=str(tmp_path)),
                timing_events=_write_events(tmp_path, nw, span=(1000 + nw) + 10 + 2 * nt))

        fit = FitTimingStep(name="fit",
                            build_design=lambda: _run_leaf(calib, 128),
                            output_path="results/fit.json")
        out = fit.run(BuildConfig(root_dir=str(tmp_path)))

        assert (calib / "params.json").exists()
        report = json.loads(out["timing_fit"].read_text())
        assert report["skipped"] == []
        assert report["fitted"][0]["component"] == "c"
        assert report["fitted"][0]["points"] == 2

    def test_a_model_that_cannot_join_is_skipped_not_fatal(self, tmp_path):
        """Only RTL collected (no pysim overlap) -> fit has no residual; skip with a report."""
        calib = tmp_path / "cal"
        tm = StreamTimingModel(component="c", calib_dir=calib)
        tm.collect_rtl(_events(128, 1183), run_id="n128")   # rtl only

        fit = FitTimingStep(name="fit", build_design=lambda: _leaf_with(calib))
        out = fit.run(BuildConfig(root_dir=str(tmp_path)))
        report = json.loads(out["timing_fit"].read_text())
        assert report["fitted"] == []
        assert report["skipped"][0]["component"] == "c"
        assert not (calib / "params.json").exists()


def _leaf_with(calib_dir):
    """A built (un-run) leaf carrying a model at *calib_dir* — enough for FitTimingStep to discover."""
    comp = _Leaf(name="c", sim=Simulation())
    comp.add_timing_model(StreamTimingModel(component="c", calib_dir=calib_dir))
    return comp


class TestCalibBusStep:
    """Calibrates the platform bus model from a traced run's m_axi ports.  The trace layer is
    monkeypatched (no VCD needed) so this tests the step's wiring: measure each maxi bundle, add the
    run to the platform corpus, refit.  `CalibBusStep.run` imports load_trace / measure_bus_span from
    their source modules, so patching the sources is what the fresh import picks up."""

    def _patch(self, monkeypatch, spans):
        import waveflow.calib.bus_model as bm
        import waveflow.utils.trace as tr

        manifest = {"boundary": [
            {"id": "gmem0", "kind": "maxi", "directions": ["read"]},
            {"id": "gmem1", "kind": "maxi", "directions": ["write"]},
        ]}
        monkeypatch.setattr(tr, "load_trace",
                            lambda *a, **k: type("BT", (), {"manifest": manifest})())
        monkeypatch.setattr(bm, "measure_bus_span",
                            lambda bt, bundle, direction: spans.get((bundle, direction)))

    def test_measures_both_bundles_and_fits(self, tmp_path, monkeypatch):
        from waveflow.build.calib_steps import CalibBusStep
        from waveflow.calib.bus_model import BusCalib

        self._patch(monkeypatch, {
            ("gmem0", "read"): {"num_trans": 8, "nwords": 128, "span": 135},
            ("gmem1", "write"): {"num_trans": 8, "nwords": 128, "span": 142},
        })
        platform = tmp_path / "platform"
        out = CalibBusStep(name="bus", platform_dir=str(platform), run_id="n128").run(
            BuildConfig(root_dir=str(tmp_path)), trace_manifest="m", trace_vcd="v")

        assert (platform / "points" / "n128.json").exists()
        bt = BusCalib(platform_dir=platform).bus_timing()
        assert bt.read_span_secs(8, 128) is not None and bt.write_span_secs(8, 128) is not None
        report = json.loads(out["bus_calibrated"].read_text())
        assert set(report["fitted_directions"]) == {"read", "write"}

    def test_a_bundle_with_no_beats_is_skipped(self, tmp_path, monkeypatch):
        """measure_bus_span returns None for an idle direction -> no point, no crash."""
        from waveflow.build.calib_steps import CalibBusStep

        self._patch(monkeypatch, {("gmem1", "write"): {"num_trans": 8, "nwords": 128, "span": 142}})
        out = CalibBusStep(name="bus", platform_dir=str(tmp_path / "p"), run_id="n128").run(
            BuildConfig(root_dir=str(tmp_path)), trace_manifest="m", trace_vcd="v")
        assert json.loads(out["bus_calibrated"].read_text())["fitted_directions"] == ["write"]

    def test_consumes_manifest_and_vcd(self):
        from waveflow.build.calib_steps import CalibBusStep
        step = CalibBusStep(name="bus", platform_dir="p", run_id="x")
        assert set(step.consumes) == {"trace_manifest", "trace_vcd"}
