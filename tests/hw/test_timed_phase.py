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
        (m_out.first: the write starts a cycle before, at 14), delivered at 18."""
        dut, out = _harness([[1, 2, 3, 4]])
        assert out == [(18.0, [2, 4, 6, 8])]
        (r,) = dut.compute_phase.records
        assert r.start == ("dut_s_in", 0, "last", 4.0)
        assert r.end == ("dut_m_out", 0, "first", 15.0)
        assert r.predicted == pytest.approx(11.0) and r.actual == pytest.approx(11.0)
        assert r.features == {"n": 4} and not r.stalled

    def test_a_buffered_burst_costs_its_beats(self):
        """The second burst is delivered at 8 and sits in the buffer while the module works; asked
        at 18, it is read at a word a cycle -- its last beat at 21, not 18 -- so its span starts at 21.
        (Decision 6: the declaration says this body is sequential.)"""
        dut, out = _harness([[1, 2, 3, 4], [5, 6, 7, 8]])
        r0, r1 = dut.compute_phase.records
        assert r1.start == ("dut_s_in", 1, "last", 21.0)
        assert r1.end[3] == pytest.approx(21.0 + 11.0)
        assert [t for t, _w in out] == [18.0, 35.0]

    def test_the_transfer_logs(self):
        dut, _ = _harness([[1, 2, 3, 4]])
        assert dut.s_in.xfer_log == [(0, 1.0, 4.0, 4, False)]
        assert dut.m_out.xfer_log == [(0, 15.0, 18.0, 4, False)]

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


# ---------------------------------------------------------------------------------------------------
# Pattern 2: the pipelined phase (Stage 4)
# ---------------------------------------------------------------------------------------------------

PIPE_SEED = {"latency": {"intercept": 5.0, "nwords": 0.0}, "fill_time": {"intercept": 0.0, "nwords": 1.0}}


@dataclass
class _PipeDoubler(FreeRunMod):
    """Pattern 2: read a burst while writing its results -- latency 5, then a word a cycle."""

    clk: Clock = field(default_factory=lambda: Clock(freq=1.0))
    swap: bool = False             # misuse: call pipeline with the endpoints the wrong way round

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in = StreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=64)
        self.m_out = StreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=64)
        self.s_b = StreamIFSlave(name=f"{self.name}_s_b", sim=self.sim, bitwidth=64)
        for ep in (self.s_in, self.m_out, self.s_b):
            self.add_endpoint(ep)
        self.phase = TimedPhase(
            "loop", spans={"latency": (self.s_in.first, self.m_out.first),
                           "fill_time": (self.m_out.first, self.m_out.last)},
            features=("nwords",), seed=PIPE_SEED)
        self.add_timing_phase(self.phase)
        self.results = []

    def run_iter(self):
        load = self.s_b.load_words(nwords_max=8) if self.swap else self.s_in.load_words(nwords_max=8)
        r = yield from self.phase.pipeline(lambda w: w * 2, load=load, store=self.m_out.store_words(),
                                           nwords=8)
        self.results.append(r)


def _pipe_harness(bursts, **kw):
    sim = Simulation()
    env = sim.env
    dut = _PipeDoubler(name="dut", sim=sim, **kw)
    drv = StreamIFMaster(name="drv", sim=sim, bitwidth=64)
    snk = StreamIFSlave(name="snk", sim=sim, bitwidth=64, queue_size=64)
    for name, m, s in (("a", drv, dut.s_in), ("b", dut.m_out, snk)):
        f = StreamIF(name=name, sim=sim, bitwidth=64, clk=Clock(freq=1.0), depth=16)
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


class TestPattern2:
    def test_the_results_start_a_latency_after_the_first_word(self):
        """8 words begun at 0: first word 1; results begin at 1 + 5 = 6 and are delivered at 14 --
        while the input is still arriving (delivered at 8): overlap, not a sequence."""
        dut, out = _pipe_harness([list(range(8))])
        assert out == [(14.0, [2 * k for k in range(8)])]
        lat, fill = dut.phase.records
        assert (lat.span, lat.start[3], lat.end[3], lat.actual) == ("latency", 1.0, 6.0, 5.0)
        assert (fill.span, fill.start[3], fill.end[3], fill.actual) == ("fill_time", 6.0, 14.0, 8.0)
        assert dut.s_in.xfer_log == [(0, 1.0, 8.0, 8, False)]

    def test_a_buffered_burst_is_read_a_word_a_cycle(self):
        """The second burst (begun 8, delivered 16) is asked at 14: read 14 .. 21, results 19 .. 27."""
        dut, out = _pipe_harness([list(range(8)), list(range(8, 16))])
        assert [t for t, _w in out] == [14.0, 27.0]
        assert dut.s_in.xfer_log[1][1:3] == (14.0, 21.0)

    def test_a_call_that_does_not_match_the_declaration_raises(self):
        with pytest.raises(RuntimeError, match="does not match its declared spans"):
            _pipe_harness([list(range(8))], swap=True)

    def test_pipeline_needs_the_two_pipeline_spans(self):
        sim = Simulation()
        a = _Doubler(name="a", sim=sim)
        with pytest.raises(RuntimeError, match="needs two spans"):
            gen = a.compute_phase.pipeline(lambda w: w, load=a.s_in.load_words(),
                                           store=a.m_out.store_words(), n=1)
            next(gen)


class TestPipelineResult:
    def test_the_result_carries_y_and_both_transfers(self):
        dut, _ = _pipe_harness([list(range(8))])
        (r,) = dut.results
        assert list(r.y) == [2 * k for k in range(8)]
        assert r.load.nwords == 8 and r.load.first == 1.0 and r.load.last == 8.0
        assert r.store.first == 6.0 and r.store.last == 14.0

    def test_a_burst_that_ends_early_is_marked_short(self):
        """5 words where 8 were requested (TLAST came early): the body sees it on r.load, and both
        records are marked short -- a legitimate run, not a sample of the law nwords=8 states."""
        dut, out = _pipe_harness([list(range(5))])
        (r,) = dut.results
        assert r.load.nwords == 5 and list(r.y) == [0, 2, 4, 6, 8]
        assert all(rec.short for rec in dut.phase.records)

    def test_a_full_burst_is_not_short(self):
        dut, _ = _pipe_harness([list(range(8))])
        assert not any(rec.short for rec in dut.phase.records)


# ---------------------------------------------------------------------------
# Deferred transfers: load_* decodes as the read it stands for, store_* checks what the compute returns
# ---------------------------------------------------------------------------

from waveflow.hw.dataschema import FloatField, IntField  # noqa: E402

Float32 = FloatField.specialize(bitwidth=32)
Int32 = IntField.specialize(bitwidth=32)


def _f32_words(x, word_bw):
    from waveflow.hw.arrayutils import write_array

    return np.asarray(write_array(np.asarray(x, np.float32), elem_type=Float32, word_bw=word_bw),
                      dtype=np.uint64)


class TestDeferredTransfers:
    def test_load_array_is_get_array_and_a_short_burst_gives_what_arrived(self):
        """Full: the n elements get_array returns.  Short (TLAST early): every element whose words
        arrived -- at two Float32 a 64-bit word, both lanes of the last word."""
        s = StreamIFSlave(name="s", sim=Simulation(), bitwidth=64)
        x = np.arange(7, dtype=np.float32)
        req = s.load_array(Float32, 7)
        assert req.nwords() == 4
        np.testing.assert_array_equal(req.decode(_f32_words(x, 64)).val, x)
        short = req.decode(_f32_words(x, 64)[:2])
        np.testing.assert_array_equal(short.val, x[:4])

    def test_a_schema_cannot_arrive_in_part(self):
        from waveflow.hw.dataschema import DataList, IntField

        class Hdr(DataList):
            elements = {"a": {"schema": IntField.specialize(32)},
                        "b": {"schema": IntField.specialize(32)}}

        s = StreamIFSlave(name="s", sim=Simulation(), bitwidth=32)
        h = Hdr(a=3, b=4)
        words = np.asarray(h.serialize(word_bw=32), dtype=np.uint64)
        got = s.load_schema(Hdr).decode(words)
        assert (int(got.a), int(got.b)) == (3, 4)
        with pytest.raises(ValueError, match="ended after 1"):
            s.load_schema(Hdr).decode(words[:1])

    def test_store_array_holds_the_compute_to_its_type_and_count(self):
        from waveflow.hw.arrayutils import array

        m = StreamIFMaster(name="m", sim=Simulation(), bitwidth=32)
        st = m.store_array(Float32, 4)
        st.check(array(Float32, np.zeros(4, np.float32)))
        st.check(array(Float32, np.zeros(2, np.float32)))           # fewer: an early TLAST upstream
        with pytest.raises(ValueError, match="returned 5 elements"):
            st.check(array(Float32, np.zeros(5, np.float32)))
        with pytest.raises(TypeError, match="Float32"):
            st.check(array(Int32, np.zeros(4, np.int32)))
        with pytest.raises(TypeError):
            st.check(np.zeros(4, np.float32))

    def test_pipeline_takes_deferred_transfers_not_endpoints(self):
        a = _Doubler(name="a", sim=Simulation())
        with pytest.raises(TypeError, match="deferred transfers"):
            next(a.compute_phase.pipeline(lambda w: w, load=a.s_in.load_words(), store=a.m_out, n=1))
