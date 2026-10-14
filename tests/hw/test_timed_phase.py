"""tests/hw/test_timed_phase.py — declared timed phases (``plans/pipelined_ops.md`` Stage 3).

A module declares a phase -- spans between its own endpoints' events, the features, a model -- and its
body waits it with ``yield phase.timeout(**features)``.  These tests hold the declared layer's rules:

* the span is measured from its start event, and the next transfer on its end endpoint closes the
  record, with the actual duration beside the predicted one;
* a sequential body's read of a burst that sat in the buffer costs its beats (decision 6 of the
  Stage 1 record) -- only on an endpoint that starts a declared span;
* the body must do what the declaration says, the features must be the declared ones, and an event
  must be on the module's own endpoint -- each refused loudly;
* an endpoint no phase names logs nothing;
* the model predicts with no simulator, from its seed or from the store.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

import numpy as np
import pytest

from waveflow.hw.clock import Clock
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.timed_phase import TimedPhase, TimingEvent
from waveflow.simulation.simulation import Simulation

SEED = {"compute_time": {"intercept": 3.0, "n": 2.0}}       # compute_time = 3 + 2 n cycles


@dataclass
class _Doubler(FreeRunMod):
    """Pattern 1: read a burst, compute, wait the declared span, write the result."""

    clk: Clock = field(default_factory=lambda: Clock(freq=1.0))
    early: bool = False            # misuse: wait the phase before reading
    wrong_feature: bool = False    # misuse: a feature the phase does not declare

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in = StreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=64)
        self.m_out = StreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=64)
        self.s_aux = StreamIFSlave(name=f"{self.name}_s_aux", sim=self.sim, bitwidth=64)
        for ep in (self.s_in, self.m_out, self.s_aux):
            self.add_endpoint(ep)
        self.compute_phase = TimedPhase(
            "compute", spans={"compute_time": (self.s_in.last, self.m_out.first)},
            features=("n",), seed=SEED)
        self.add_timing_phase(self.compute_phase)

    def run_iter(self):
        if self.early:
            yield self.compute_phase.timeout(n=1)
        w = yield from self.s_in.get()
        if self.wrong_feature:
            yield self.compute_phase.timeout(m=len(w))
        yield self.compute_phase.timeout(n=len(w))
        yield from self.m_out.write(w * 2)


def _harness(bursts, depth=16, **kw):
    """The doubler between a driver and a sink at 1 Hz (times are cycles).  Returns the module and
    ``[(delivery time, words)]`` of what came out."""
    sim = Simulation()
    env = sim.env
    dut = _Doubler(name="dut", sim=sim, **kw)
    drv = StreamIFMaster(name="drv", sim=sim, bitwidth=64)
    snk = StreamIFSlave(name="snk", sim=sim, bitwidth=64, queue_size=64)
    for name, m, s in (("a", drv, dut.s_in), ("b", dut.m_out, snk)):
        f = StreamIF(name=name, sim=sim, bitwidth=64, clk=Clock(freq=1.0), depth=depth)
        f.bind("master", m)
        f.bind("slave", s)
    out = []

    def driver():
        for b in bursts:
            yield from drv.write(np.asarray(b, dtype=np.uint64))

    def sink():
        for _ in bursts:
            w = yield from snk.get()
            out.append((env.now, list(w)))

    env.process(driver())
    env.process(sink())
    env.process(dut._run_iter_forever())
    env.run(until=500)
    return dut, out


class TestPattern1:
    def test_the_span_is_waited_and_recorded(self):
        """4 words land at 4 (s_in.last); compute_time = 3 + 2*4 = 11; the result's first beat at 15
        (m_out.first), delivered at 19."""
        dut, out = _harness([[1, 2, 3, 4]])
        assert out == [(19.0, [2, 4, 6, 8])]
        (r,) = dut.compute_phase.records
        assert r.start == ("dut_s_in", 0, "last", 4.0)
        assert r.end == ("dut_m_out", 0, "first", 15.0)
        assert r.predicted == pytest.approx(11.0) and r.actual == pytest.approx(11.0)
        assert r.features == {"n": 4} and not r.stalled

    def test_a_buffered_burst_costs_its_beats(self):
        """The second burst is delivered at 8 and sits in the buffer while the module works; asked
        at 19, it is read at a word a cycle -- its last beat at 22, not 19 -- so its span starts at 22.
        (Decision 6: the declaration says this body is sequential.)"""
        dut, out = _harness([[1, 2, 3, 4], [5, 6, 7, 8]])
        r0, r1 = dut.compute_phase.records
        assert r1.start == ("dut_s_in", 1, "last", 22.0)
        assert r1.end[3] == pytest.approx(22.0 + 11.0)
        assert [t for t, _w in out] == [19.0, 37.0]

    def test_the_transfer_logs(self):
        dut, _ = _harness([[1, 2, 3, 4]])
        assert dut.s_in.xfer_log == [(0, 1.0, 4.0, 4, False)]
        assert dut.m_out.xfer_log == [(0, 15.0, 19.0, 4, False)]

    def test_an_undeclared_endpoint_logs_nothing(self):
        dut, _ = _harness([[1, 2]])
        assert dut.s_aux.xfer_log is None and not dut.s_aux._charge_beats


class TestRefusals:
    def test_timeout_before_the_start_transfer_raises(self):
        with pytest.raises(RuntimeError, match="no transfer since this phase last ran"):
            _harness([[1, 2]], early=True)

    def test_an_undeclared_feature_raises(self):
        with pytest.raises(TypeError, match=r"declares features \['n'\]"):
            _harness([[1, 2]], wrong_feature=True)

    def test_an_event_on_another_modules_endpoint_raises(self):
        sim = Simulation()
        a = _Doubler(name="a", sim=sim)
        b = _Doubler(name="b", sim=sim)
        ph = TimedPhase("x", spans={"t": (a.s_in.last, b.m_out.first)})
        with pytest.raises(ValueError, match="not the module's own"):
            a.add_timing_phase(ph)

    def test_a_duplicate_name_raises_and_registration_returns_none(self):
        sim = Simulation()
        a = _Doubler(name="a", sim=sim)
        ph = TimedPhase("other", spans={"t": (a.s_aux.last, a.m_out.first)})
        assert a.add_timing_phase(ph) is None
        with pytest.raises(ValueError, match="already has a timed phase"):
            a.add_timing_phase(TimedPhase("other", spans={"t": (a.s_aux.last, a.m_out.first)}))

    def test_a_span_is_a_pair_of_events(self):
        sim = Simulation()
        a = _Doubler(name="a", sim=sim)
        with pytest.raises(TypeError, match="a span is"):
            TimedPhase("x", spans={"t": (a.s_in, a.m_out)})
        with pytest.raises(ValueError, match="'first' or 'last'"):
            TimingEvent(a.s_in, "middle")


class TestTheModel:
    def test_predicts_with_no_simulator(self):
        sim = Simulation()
        a = _Doubler(name="a", sim=sim)
        assert a.compute_phase.predict(n=4) == {"compute_time": pytest.approx(11.0)}

    def test_endpoint_events_are_cached(self):
        sim = Simulation()
        a = _Doubler(name="a", sim=sim)
        assert a.s_in.first is a.s_in.first and a.s_in.last is not a.s_in.first

    def test_fitted_parameters_come_from_the_store(self, tmp_path):
        """A phase pointed at a calibration directory predicts from what is filed there, not its seed."""
        sim = Simulation()
        a = _Doubler(name="a", sim=sim)
        (tmp_path / "compute_time.json").write_text(json.dumps({"n": 1.0, "intercept": 40.0}),
                                                    encoding="utf-8")
        ph = TimedPhase("stored", spans={"compute_time": (a.s_aux.last, a.m_out.first)},
                        features=("n",), seed=SEED, calib_dir=tmp_path)
        assert ph.predict(n=4) == {"compute_time": pytest.approx(44.0)}
