"""tests/hw/test_freerun_timing.py — FreeRunComp's opt-in per-firing timing record.

Attaching a TimingModel turns on recording: the base loop times each firing and keeps a row of
{features, current_dly, span} whenever the body called `timed_delay`.  A component with no model
takes the plain path and pays nothing.  See plans/timing_model.md.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from waveflow.calib.timing_model import StreamTimingModel
from waveflow.hw.hw_freerun import FreeRunComp, discover_timing_models
from waveflow.simulation.simulation import Simulation


@dataclass
class _Leaf(FreeRunComp):
    """A minimal leaf: each firing does `base` units of work, then a model-predicted delay."""
    n_firings: int = 3
    base: float = 10.0
    _count: int = field(default=0)

    def run_iter(self):
        if self._count >= self.n_firings:
            yield self.timeout(10_000)          # idle; the `until` bound cuts this off
            return
        self._count += 1
        yield self.timeout(self.base)           # the "work"
        yield self.timeout(self.timed_delay({"nwords": 128, "num_trans": 8}))


def _run(comp, sim, until):
    sim.env.process(comp._run_iter_forever())
    sim.env.run(until=until)


class TestRecording:
    def test_firings_are_recorded_with_span_features_and_dly(self, tmp_path):
        sim = Simulation()
        comp = _Leaf(name="c", sim=sim, n_firings=3, base=10.0)
        # clk=None -> predict returns cycles; seed gives a constant 5-cycle delay.
        tm = StreamTimingModel(component="c", calib_dir=tmp_path,
                               seed={"nwords": 0.0, "num_trans": 0.0, "intercept": 5.0})
        comp.add_timing_model(tm)

        _run(comp, sim, until=100)

        assert len(comp.firing_records) == 3
        for r in comp.firing_records:
            assert r["span"] == pytest.approx(15.0)      # 10 work + 5 delay
            assert r["current_dly"] == pytest.approx(5.0)
            assert r["nwords"] == 128 and r["num_trans"] == 8

    def test_records_feed_collect_pysim(self, tmp_path):
        """The whole point: firing_records is exactly what collect_pysim consumes."""
        sim = Simulation()
        comp = _Leaf(name="c", sim=sim, n_firings=4, base=8.0)
        tm = StreamTimingModel(component="c", calib_dir=tmp_path,
                               seed={"nwords": 0.0, "num_trans": 0.0, "intercept": 0.0})
        comp.add_timing_model(tm)
        _run(comp, sim, until=100)

        tm.collect_pysim(comp.firing_records, run_id="n128")
        pt = tm.get_params(tm.calib_dir / "pysim" / "n128", validate=False)
        assert pt["span"] == pytest.approx(8.0)          # median firing span
        assert pt["current_dly"] == pytest.approx(0.0)


class TestNoModel:
    def test_uncalibrated_component_takes_the_plain_path(self):
        """No model attached: no firing_records, no error — the loop is unchanged."""
        sim = Simulation()
        comp = _Leaf(name="c", sim=sim, n_firings=2, base=5.0)
        assert comp.timing_model is None
        _run(comp, sim, until=30)
        assert not hasattr(comp, "firing_records")

    def test_timed_delay_is_a_noop_without_a_model(self):
        sim = Simulation()
        comp = _Leaf(name="c", sim=sim)
        assert comp.timed_delay({"nwords": 128, "num_trans": 8}) == 0.0


class TestDiscovery:
    def test_finds_models_across_the_tree(self):
        @dataclass
        class Child(FreeRunComp):
            def run_iter(self):
                yield self.timeout(1)

        @dataclass
        class Parent(FreeRunComp):
            def __post_init__(self):
                super().__post_init__()
                self.add_comp(Child(name=f"{self.name}_kid", sim=self.sim))

        sim = Simulation()
        parent = Parent(name="p", sim=sim)
        child = parent.sub_comps["p_kid"]

        class _FakeTM:
            pass

        tm = _FakeTM()
        child.add_timing_model(tm)

        found = discover_timing_models(parent)
        assert found == [(child, tm)]

    def test_empty_when_none_attached(self):
        @dataclass
        class Leaf(FreeRunComp):
            def run_iter(self):
                yield self.timeout(1)

        assert discover_timing_models(Leaf(name="c", sim=Simulation())) == []
