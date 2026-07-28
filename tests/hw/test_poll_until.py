"""Unit tests for the LT polling-overhead model (``plans/poll_until_lt_model.md``).

Covers the three non-obvious correctness points:

* :class:`PollCond` / :class:`Eq` / :class:`Ne` sim evaluation,
* :meth:`MMIFMaster.poll_until` — the deterministic-mean discovery delay and the
  O(transactions) write-notify wait (blocks until a write makes ``cond`` true),
* the per-bus occupancy derating ``1/(1-ov)`` on the ``nwords`` term only, the
  ``ov``-per-slave isolation, and the clamp-and-warn at the ``0.99`` ceiling.
"""
from __future__ import annotations

import warnings

import numpy as np
import pytest

from waveflow.hw.clock import Clock
from waveflow.hw.memif import (
    AXIMMCrossBarIF,
    Eq,
    MMIFMaster,
    Ne,
    PollCond,
    assign_address_ranges,
)
from waveflow.hw.memory import MemoryMod
from waveflow.simulation.simulation import Simulation


# ---------------------------------------------------------------------------
# Harness: one or two masters on a crossbar over a MemoryMod
# ---------------------------------------------------------------------------

def _setup(
    *,
    freq=1.0,
    nports_master=1,
    latency_init=0.0,
    latency_read_return=0.0,
    mem_latency_init=0.0,
    mem_latency_per_word=0.0,
    base=0x1000,
    nwords=64,
    mem_bw=32,
):
    sim = Simulation()
    clk = Clock(freq=freq)
    mem = MemoryMod(
        sim=sim, word_size=mem_bw, inline=False, clk=clk,
        latency_init=mem_latency_init, latency_per_word=mem_latency_per_word,
    )
    mem.alloc(nwords)
    xbar = AXIMMCrossBarIF(
        sim=sim, clk=clk, nports_master=nports_master, nports_slave=1,
        bitwidth=mem_bw, latency_init=latency_init,
        latency_read_return=latency_read_return,
    )
    masters = [MMIFMaster(sim=sim, bitwidth=mem_bw) for _ in range(nports_master)]
    for i, m in enumerate(masters):
        xbar.bind(f"master_{i}", m)
    xbar.bind("slave_0", mem.s_mm)
    assign_address_ranges([mem.s_mm], [(base, nwords * (mem_bw // 8))])
    return sim, clk, xbar, mem, masters, base


# ---------------------------------------------------------------------------
# PollCond
# ---------------------------------------------------------------------------

class TestPollCond:
    def test_eq_ne_eval(self):
        assert Eq(5).eval(5) and not Eq(5).eval(6)
        assert Ne(5).eval(6) and not Ne(5).eval(5)
        # rhs may be a prior runtime-read value (still a plain int in sim)
        head = np.uint32(3)
        assert Ne(head).eval(4) and not Ne(head).eval(3)

    def test_ops_are_fixed_per_subclass(self):
        assert Eq(0).op == "==" and Ne(0).op == "!="

    def test_unknown_op_raises(self):
        with pytest.raises(ValueError, match="unsupported op"):
            PollCond(0).eval(0)


# ---------------------------------------------------------------------------
# poll_until — discovery delay + write-notify wait
# ---------------------------------------------------------------------------

class TestPollUntilTiming:
    def test_discovery_delay_when_already_true(self):
        """cond already satisfied at entry → only the deterministic mean
        discovery delay (poll_interval-1)/2 cycles is added."""
        sim, clk, xbar, mem, (master,), base = _setup(freq=1.0)
        mem._mem.write(0, np.array([7], dtype=np.uint32))
        out = {}

        def proc():
            t0 = master.now
            out["v"] = yield from master.poll_until(base, Eq(7), poll_interval=11)
            out["dt"] = master.now - t0

        sim.env.process(proc())
        sim.env.run()
        assert out["v"] == 7
        assert out["dt"] == pytest.approx((11 - 1) / 2)  # 5 cycles / 1 Hz

    def test_blocks_until_write_makes_cond_true(self):
        """poll_until wakes on the write that satisfies cond (the exact event
        time T), then adds the discovery delay — not stepping every interval."""
        sim, clk, xbar, mem, (writer, poller), base = _setup(freq=1.0, nports_master=2)
        mem._mem.write(0, np.array([0], dtype=np.uint32))
        out = {}

        def writer_proc():
            yield writer.timeout(20.0)              # event becomes true at t=20
            yield from writer.write(np.array([42], dtype=np.uint32), base)

        def poller_proc():
            t0 = poller.now
            out["v"] = yield from poller.poll_until(base, Eq(42), poll_interval=7)
            out["t_done"] = poller.now
            out["t0"] = t0

        sim.env.process(writer_proc())
        sim.env.process(poller_proc())
        sim.env.run()
        assert out["v"] == 42
        # The poller is active while the writer writes, so its ov = 1/7 derates
        # that write's single word (bandwidth steal slows the very write it
        # awaits): write completes at 20 + 1/(1-1/7); then +(7-1)/2 = 3 cycles
        # discovery.  This is the model's two costs both showing up at once.
        assert out["t_done"] == pytest.approx(
            20.0 + 1.0 / (1.0 - 1.0 / 7.0) + (7 - 1) / 2
        )

    def test_zero_discovery_for_interval_one(self):
        sim, clk, xbar, mem, (master,), base = _setup(freq=1.0)
        mem._mem.write(0, np.array([1], dtype=np.uint32))
        out = {}

        def proc():
            t0 = master.now
            yield from master.poll_until(base, Eq(1), poll_interval=1)
            out["dt"] = master.now - t0

        sim.env.process(proc())
        sim.env.run()
        assert out["dt"] == 0.0  # (1-1)/2 = 0


# ---------------------------------------------------------------------------
# Occupancy derating
# ---------------------------------------------------------------------------

class TestDerating:
    def test_ov_sums_per_bus_and_clamps_with_one_warning(self):
        sim, clk, xbar, mem, (master,), base = _setup()
        # one poller, interval 2, cost 1 -> ov = 0.5 -> stretch 2.0
        xbar.register_poller(base, 1, 2)
        assert xbar._poll_stretch("slave_0") == pytest.approx(2.0)
        # a second identical poller -> ov = 1.0 -> clamp to 0.99, warn once
        xbar.register_poller(base, 1, 2)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            st1 = xbar._poll_stretch("slave_0")
            st2 = xbar._poll_stretch("slave_0")
        assert st1 == pytest.approx(1.0 / (1.0 - 0.99))
        assert st1 == st2
        # warn once per saturation onset, not per evaluation
        msgs = [w for w in caught if "polling-bound" in str(w.message)]
        assert len(msgs) == 1

    def test_read_per_word_term_derated_init_untouched(self):
        """A read's nwords term is stretched by 1/(1-ov); the fixed
        latency_read_return is not."""
        sim, clk, xbar, mem, (master,), base = _setup(
            freq=1.0, latency_init=0.0, latency_read_return=3.0,
        )
        # ov = 0.5 -> stretch 2.0 (register directly; no separate poller process)
        xbar.register_poller(base, 1, 2)
        out = {}

        def proc():
            t0 = master.now
            yield from master.read(4, base)   # 4 words
            out["dt"] = master.now - t0

        sim.env.process(proc())
        sim.env.run()
        # req leg = latency_init/freq = 0; mem access = 0; ret_dly =
        # (latency_read_return + nwords*stretch)/freq = (3 + 4*2) = 11.
        assert out["dt"] == pytest.approx(3.0 + 4 * 2.0)

    def test_independent_buses_do_not_derate_each_other(self):
        """A poller on one slave must not stretch transfers to another slave."""
        sim = Simulation()
        clk = Clock(freq=1.0)
        mem0 = MemoryMod(sim=sim, word_size=32, inline=False, clk=clk)
        mem1 = MemoryMod(sim=sim, word_size=32, inline=False, clk=clk)
        mem0.alloc(16)
        mem1.alloc(16)
        xbar = AXIMMCrossBarIF(
            sim=sim, clk=clk, nports_master=1, nports_slave=2, bitwidth=32,
            latency_init=0.0, latency_read_return=0.0,
        )
        master = MMIFMaster(sim=sim, bitwidth=32)
        xbar.bind("master_0", master)
        xbar.bind("slave_0", mem0.s_mm)
        xbar.bind("slave_1", mem1.s_mm)
        assign_address_ranges([mem0.s_mm, mem1.s_mm], [(0x0, 64), (0x1000, 64)])
        # heavy poller on slave_0; a read on slave_1 must be undisturbed.
        xbar.register_poller(0x0, 1, 2)        # ov(slave_0) = 0.5
        assert xbar._poll_stretch("slave_0") == pytest.approx(2.0)
        assert xbar._poll_stretch("slave_1") == pytest.approx(1.0)  # ov = 0
        out = {}

        def proc():
            t0 = master.now
            yield from master.read(4, 0x1000)  # slave_1, not derated
            out["dt"] = master.now - t0

        sim.env.process(proc())
        sim.env.run()
        assert out["dt"] == pytest.approx(4.0)   # 4 words * stretch 1.0
