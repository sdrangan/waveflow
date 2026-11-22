"""The loop check (``waveflow.hw.loop_check``): a link on a composite's cycle that carried a burst larger
than its depth -- where pysim, which moves bursts, can pass and RTL, which moves words, can deadlock.

The approximation it guards against, shown directly; the ring stub with its own depths (clean) and with a
feedback link shorter than a frame (warned, while pysim still answers every job); a link into a unit
that drains its inputs (exempt); and an oversize burst on a link that is on no cycle (not warned).
"""
from __future__ import annotations

import warnings

import numpy as np
import pytest

import examples.ring_stub.ring_stub as rs
from waveflow.hw.clock import Clock
from waveflow.hw.harness import harness
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.loop_check import LoopDepthWarning, loop_depth_hazards
from waveflow.simulation.simulation import Simulation


def test_pysim_admits_a_burst_larger_than_its_link_with_no_reader():
    """The approximation itself: a 16-word write into a depth-4 link completes though nothing reads.
    RTL's writer would stall after 4 words.  Harmless in a pipeline; on a cycle, the check's case."""
    sim = Simulation()
    tx = StreamIFMaster(name="tx", sim=sim, bitwidth=32)
    rx = StreamIFSlave(name="rx", sim=sim, bitwidth=32)
    link = StreamIF(name="l", sim=sim, clk=Clock(freq=1e8), bitwidth=32, depth=4)
    link.bind("master", tx)
    link.bind("slave", rx)
    done = []

    def prod():
        yield from tx.write(np.arange(16, dtype=np.uint32))
        done.append(True)

    sim.env.process(prod())
    sim.env.run(until=1e-5)
    assert done and link.max_burst == 16


def test_the_ring_stub_at_its_own_depths_has_no_hazard():
    with warnings.catch_warnings():
        warnings.simplefilter("error", LoopDepthWarning)
        sysm = harness(rs.RingStub, workload="long")
        sysm.run()
    assert sysm.dut().loop_depth_hazards == []


def test_a_feedback_link_shorter_than_a_frame_is_named_though_pysim_passes(monkeypatch):
    """The case the build run of plans/shared_kernel.md Stage 7 met: with the credit intact and the
    feedback link cut below a frame, pysim still answers every job -- and the check names the link."""
    monkeypatch.setattr(rs, "DEPTH", {**rs.DEPTH, "fb": 8})
    sysm = harness(rs.RingStub, workload="long")
    with pytest.warns(LoopDepthWarning, match="ring_stub_fb_if"):
        sysm.run()
    assert len(sysm.stand_ins["m_status"].words) == len(rs.jobs("long"))     # pysim: every job answered
    (h,) = sysm.dut().loop_depth_hazards
    assert (h.depth, h.max_burst) == (8, 64)


def test_a_link_into_a_unit_that_drains_its_inputs_is_exempt(monkeypatch):
    """The same short link, but its reader declares drains_inputs: its writer can stall only while the
    reader's own output link is full, which is judged in its own right."""
    monkeypatch.setattr(rs, "DEPTH", {**rs.DEPTH, "fb": 8})
    monkeypatch.setattr(rs.Mux, "drains_inputs", True, raising=False)
    sysm = harness(rs.RingStub, workload="long")
    sysm.run()
    assert sysm.dut().loop_depth_hazards == []


def test_an_oversize_burst_on_no_cycle_is_not_a_hazard():
    """No cycle, no hazard: block_eq is a pipeline (broadcast, gain, apply), and the check only asks
    about links on cycles."""
    from examples.block_eq.block_eq import BlockEq

    sysm = harness(BlockEq, workload="test")
    sysm.run()
    assert loop_depth_hazards(sysm.dut()) == []
