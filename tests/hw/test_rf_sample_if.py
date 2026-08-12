"""Unit tests for :mod:`waveflow.hw.rf_sample_if` — the RF-domain block-rate sample channel.

Two things are being pinned down here, and only one of them is ordinary coverage.

The ordinary half is the **loss contract**: underrun and overrun have to be observable numbers, and
each has to have been non-zero in a test that predicted the exact value.  A counter that has never
counted is not evidence.

The other half is :class:`TestMetronome`, which is a **deliverable, not a check**.  The claim
``guide/rf/sampling.md`` makes — that a relative ``timeout`` loop slips cumulatively and an absolute
grid does not — is only honest if the failure has been demonstrated.  So the first test builds the
rejected scheduler and shows it drifting, and the second runs the real one through the same yielding
body and shows the grid held.
"""
from __future__ import annotations

import numpy as np
import pytest
from dataclasses import dataclass

from waveflow.hw.clock import Clock
from waveflow.hw.rf_sample_if import RFSampIF, RFSampIFRx, RFSampIFTx
from waveflow.simulation.simobj import ProcessGen
from waveflow.simulation.simulation import Simulation


# --------------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------------

@dataclass
class TracingRFSampIF(RFSampIF):
    """An :class:`RFSampIF` whose block body **yields** and records when it started.

    ``_drain_one`` is the documented seam for exactly this: the grid discipline lives in
    ``run_proc`` and the transfer lives here, so a body that costs time can be substituted without
    touching the scheduler under test.  ``body_delay`` stands in for anything real that yields — a
    charged transfer, a callback, a blocking push.
    """

    body_delay: float = 0.0

    def __post_init__(self) -> None:
        super().__post_init__()
        #: ``env.now`` at the top of each block body — i.e. when the metronome actually fired.
        self.ticks: list[float] = []

    def _drain_one(self, k: int) -> ProcessGen[None]:
        self.ticks.append(float(self.env.now))
        if self.body_delay:
            yield self.timeout(float(self.body_delay))
        yield from super()._drain_one(k)


def _edge(sim, *, blksize=8, n_ch=1, samp_rate=8.0, depth=2, rx_depth=64, n_blk=6,
          cls=RFSampIF, **kw):
    """Build a bound tx--interface--rx triple.  ``blksize/samp_rate`` is the block period."""
    clk = Clock(name="samp_clk", freq=float(samp_rate))
    iface = cls(name="rf_if", sim=sim, samp_clk=clk, n_ch=n_ch, blksize=blksize, depth=depth,
                n_blk=n_blk, **kw)
    tx = RFSampIFTx(name="tx_ep", sim=sim)
    rx = RFSampIFRx(name="rx_ep", sim=sim, depth=rx_depth)
    iface.bind("tx", tx)
    iface.bind("rx", rx)
    return iface, tx, rx


def _ramp(iface, k: int) -> np.ndarray:
    """A distinguishable block: channel *c*, sample *i* holds ``1000*k + 100*c + i``."""
    n_ch, n = int(iface.n_ch), int(iface.blksize)
    return (1000 * k + 100 * np.arange(n_ch)[:, None] + np.arange(n)[None, :]).astype(float)


def _feeder(iface, tx, nblocks, delay=0.0):
    """A producer process: after *delay*, put *nblocks* blocks as fast as backpressure allows."""
    def proc():
        if delay:
            yield iface.timeout(delay)
        for k in range(1, nblocks + 1):
            yield from tx.put(_ramp(iface, k))
    return proc


# --------------------------------------------------------------------------------------------
# Gate 4 — the metronome, in two halves
# --------------------------------------------------------------------------------------------

class TestMetronome:
    """The absolute-grid schedule, shown against the scheduler it replaces."""

    T = 1.0          # block period, seconds
    DELTA = 0.1      # what the body costs
    N = 6            # blocks

    def test_relative_timeout_loop_slips_cumulatively(self):
        """(a) The **rejected** scheduler, demonstrated failing.

        ``while True: yield timeout(period); <body that yields>`` restarts each period from a later
        ``env.now``, so every block period the body costs is added to the grid and never given back.
        The drift is not a constant offset — it grows without bound with the block index, which is
        what makes it fatal for a sample clock rather than merely untidy.
        """
        sim = Simulation()
        env = sim.env
        fires: list[float] = []

        def naive_metronome():
            while len(fires) < self.N:
                yield env.timeout(self.T)          # <-- relative: the bug
                fires.append(float(env.now))
                yield env.timeout(self.DELTA)      # the body yields

        env.process(naive_metronome())
        env.run()

        # Block k fires at k*T + (k-1)*DELTA, not at k*T.
        assert fires == pytest.approx([k * self.T + (k - 1) * self.DELTA
                                       for k in range(1, self.N + 1)])
        # Cumulative: the error at block k is proportional to k, not constant.
        drift = [f - k * self.T for k, f in enumerate(fires, start=1)]
        assert drift == pytest.approx([(k - 1) * self.DELTA for k in range(1, self.N + 1)])
        assert drift[-1] > drift[0]
        assert drift[-1] == pytest.approx((self.N - 1) * self.DELTA)

    def test_absolute_grid_holds_under_the_same_yielding_body(self):
        """(b) The **real** ``RFSampIF``, same body cost, grid intact.

        Every block still fires at ``t_epoch + k * blk_period``, so after ``N`` blocks the grid has
        accumulated exactly zero error where the relative loop had accumulated ``(N-1) * DELTA``.
        """
        sim = Simulation()
        blksize, samp_rate = 8, 8.0 / self.T          # -> blk_period == T
        iface, tx, rx = _edge(sim, blksize=blksize, samp_rate=samp_rate, n_blk=self.N,
                              cls=TracingRFSampIF, body_delay=self.DELTA)
        sim.env.process(_feeder(iface, tx, self.N)())
        sim.run_sim()

        assert iface.blk_period == pytest.approx(self.T)
        expect = [iface.t0 + k * self.T for k in range(1, self.N + 1)]
        assert iface.ticks == pytest.approx(expect)
        # The paired claim, stated as a number: the relative loop would have ended (N-1)*DELTA late.
        assert iface.ticks[-1] == pytest.approx(self.N * self.T)
        assert iface.ticks[-1] != pytest.approx(self.N * self.T + (self.N - 1) * self.DELTA)
        # ...and the grid is intact all the way through: every gap is exactly one period.
        gaps = np.diff(iface.ticks)
        assert gaps == pytest.approx([self.T] * (self.N - 1))
        iface.assert_clean()

    def test_grid_is_anchored_at_t0_not_at_the_first_block(self):
        """A non-zero epoch shifts the whole grid; it does not merely delay the first block."""
        sim = Simulation()
        t0 = 3.25
        iface, tx, rx = _edge(sim, blksize=8, samp_rate=8.0, n_blk=4, cls=TracingRFSampIF)
        iface.set_t0(t0, owner="test")
        sim.env.process(_feeder(iface, tx, 4)())
        sim.run_sim()
        assert iface.ticks == pytest.approx([t0 + k * 1.0 for k in range(1, 5)])

    def test_a_body_longer_than_a_block_period_fails_loud(self):
        """The metronome cannot silently fall behind: an over-long body is an error, not a slip."""
        sim = Simulation()
        iface, tx, rx = _edge(sim, blksize=8, samp_rate=8.0, n_blk=3, cls=TracingRFSampIF,
                              body_delay=1.5)          # > blk_period of 1.0
        sim.env.process(_feeder(iface, tx, 3)())
        with pytest.raises(RuntimeError, match="cannot hold its sample grid"):
            sim.run_sim()


# --------------------------------------------------------------------------------------------
# Gate 3 — the counters, made non-vacuous
# --------------------------------------------------------------------------------------------

class TestLossContract:

    def test_clean_run_has_zero_underrun_and_overrun(self):
        sim = Simulation()
        iface, tx, rx = _edge(sim, n_blk=5)
        sim.env.process(_feeder(iface, tx, 5)())
        sim.run_sim()
        assert iface.counters() == {"blocks_sent": 5, "blocks_delivered": 5,
                                    "underrun": 0, "overrun": 0}
        iface.assert_clean()

    def test_late_producer_underruns_and_the_padding_is_zeros(self):
        """A producer that starts 2.5 block periods late misses exactly the first two periods.

        The count is the contract; the zero-fill is checked too, because the padding has to be
        *visible* in the RF output rather than a repeat of the last block or uninitialised memory.
        """
        sim = Simulation()
        iface, tx, rx = _edge(sim, blksize=8, samp_rate=8.0, n_blk=6, depth=2)
        sim.env.process(_feeder(iface, tx, 6, delay=2.5)())
        sim.run_sim()

        assert iface.underrun == 2
        assert iface.overrun == 0
        assert iface.blocks_sent == 6
        got = list(rx.rx_queue.items)
        assert [b.idx for b in got] == [1, 2, 3, 4, 5, 6]
        assert np.array_equal(got[0].data, np.zeros((1, 8)))
        assert np.array_equal(got[1].data, np.zeros((1, 8)))
        assert np.array_equal(got[2].data, _ramp(iface, 1))     # real data resumes, from block 1

    def test_full_receiver_overruns_and_drops(self):
        """A receiver that never drains accepts exactly ``depth`` blocks; the rest are dropped."""
        sim = Simulation()
        rx_depth, n_blk = 3, 7
        iface, tx, rx = _edge(sim, n_blk=n_blk, rx_depth=rx_depth)   # nothing consumes rx.rx_queue
        sim.env.process(_feeder(iface, tx, n_blk)())
        sim.run_sim()

        assert iface.blocks_delivered == rx_depth
        assert iface.overrun == n_blk - rx_depth
        assert iface.underrun == 0
        assert iface.blocks_sent == iface.blocks_delivered + iface.overrun
        # A drop leaves a GAP in the grid indices — loss is visible in the data, not only the count.
        assert [b.idx for b in rx.rx_queue.items] == [1, 2, 3]

    def test_assert_clean_raises_and_names_both_numbers(self):
        sim = Simulation()
        iface, tx, rx = _edge(sim, n_blk=4, rx_depth=1)
        sim.env.process(_feeder(iface, tx, 4)())
        sim.run_sim()
        with pytest.raises(AssertionError, match=r"overrun=3"):
            iface.assert_clean()


# --------------------------------------------------------------------------------------------
# Backpressure — the other half of the asymmetry
# --------------------------------------------------------------------------------------------

class TestBackpressure:

    def test_put_yields_when_the_buffer_is_full(self):
        """The producer runs at most ``depth`` blocks ahead — bounded lookahead, not free-running.

        This is the legitimate half of the asymmetry: over-production *is* signalled (the converter
        has a real input FIFO and it stalls the fabric), which is why ``put`` may yield and delivery
        may not.
        """
        sim = Simulation()
        depth, n_blk = 2, 5
        iface, tx, rx = _edge(sim, blksize=8, samp_rate=8.0, n_blk=n_blk, depth=depth)
        put_times: list[float] = []

        def feeder():
            for k in range(1, n_blk + 1):
                yield from tx.put(_ramp(iface, k))
                put_times.append(float(sim.env.now))

        sim.env.process(feeder())
        sim.run_sim()

        # depth blocks go straight in at t=0; each later put waits for the metronome to free a slot.
        assert put_times[:depth] == pytest.approx([0.0] * depth)
        assert put_times[depth:] == pytest.approx([1.0, 2.0, 3.0])
        assert iface.n_buffered <= depth
        iface.assert_clean()


# --------------------------------------------------------------------------------------------
# Structure: all channels on one edge, t0 as a vector, single ownership
# --------------------------------------------------------------------------------------------

class TestStructure:

    def test_one_event_carries_every_channel(self):
        sim = Simulation()
        n_ch, blksize, n_blk = 4, 8, 3
        iface, tx, rx = _edge(sim, n_ch=n_ch, blksize=blksize, n_blk=n_blk)
        sim.env.process(_feeder(iface, tx, n_blk)())
        sim.run_sim()
        got = list(rx.rx_queue.items)
        assert len(got) == n_blk                       # n_blk events, NOT n_blk * n_ch
        assert all(b.data.shape == (n_ch, blksize) for b in got)
        assert np.array_equal(got[0].data, _ramp(iface, 1))

    def test_a_block_of_the_wrong_shape_is_refused(self):
        sim = Simulation()
        iface, tx, rx = _edge(sim, n_ch=2, blksize=8, n_blk=1)

        def bad():
            yield from tx.put(np.zeros((1, 8)))        # one channel, not two

        sim.env.process(bad())
        with pytest.raises(ValueError, match=r"a block must be exactly \(2, 8\)"):
            sim.run_sim()

    def test_t0_is_a_scalar_and_samp_time_derives_from_it(self):
        """One epoch per tile, shared by every channel it carries."""
        sim = Simulation()
        iface, tx, rx = _edge(sim, n_ch=3, blksize=8, samp_rate=8.0, n_blk=1)
        iface.set_t0(0.5, owner="tile")
        assert iface.t0 == pytest.approx(0.5)
        # sample n is at t0 + n/fs -- derived, not scheduled, and the same for every channel.
        assert iface.samp_time(8) == pytest.approx(1.5)
        assert iface.samp_time(0) == pytest.approx(0.5)

    def test_t0_defaults_to_zero_when_no_converter_set_it(self):
        sim = Simulation()
        iface, _, _ = _edge(sim, n_ch=4, n_blk=1)
        assert iface.t0 == pytest.approx(0.0)
        iface.set_t0(2.0, owner="tile")
        assert iface.t0 == pytest.approx(2.0)

    def test_t0_has_exactly_one_owner(self):
        """Two declarations that can disagree is the bug this refuses."""
        sim = Simulation()
        iface, _, _ = _edge(sim, n_ch=2, n_blk=1)
        iface.set_t0(1.0, owner="tile_a")
        iface.set_t0(1.0, owner="tile_a")              # same owner: fine, it is one source
        with pytest.raises(ValueError, match="t0 has exactly one owner"):
            iface.set_t0(2.0, owner="tile_b")

    def test_a_per_channel_t0_vector_is_refused(self):
        """t0 is an *epoch* (a tile property); per-channel skew is a *delay* (a path property).

        An earlier draft accepted a vector here, and the transport ignored it -- every channel rides
        one block delivered by one event, so no per-channel offset could change when samples arrive.
        A field that can only be recorded and never applied does not belong on the edge, so it is
        refused rather than silently inert.
        """
        sim = Simulation()
        iface, _, _ = _edge(sim, n_ch=3, n_blk=1)
        with pytest.raises(ValueError, match="scalar epoch"):
            iface.set_t0([0.0, 0.25, 0.5], owner="tile")

    def test_samp_rate_and_blksize_are_read_through_the_interface(self):
        """Endpoints restate nothing: there is one declaration of each quantity."""
        sim = Simulation()
        iface, tx, rx = _edge(sim, n_ch=2, blksize=16, samp_rate=32.0, n_blk=1)
        assert (tx.n_ch, tx.blksize, tx.samp_rate) == (2, 16, 32.0)
        assert (rx.n_ch, rx.blksize, rx.samp_rate) == (2, 16, 32.0)
        assert iface.blk_period == pytest.approx(0.5)

    def test_an_unbound_endpoint_says_so(self):
        sim = Simulation()
        tx = RFSampIFTx(name="lonely", sim=sim)
        with pytest.raises(RuntimeError, match="not bound to an RFSampIF"):
            _ = tx.blksize

    def test_binding_rejects_the_wrong_endpoint_type(self):
        sim = Simulation()
        iface = RFSampIF(name="if", sim=sim, samp_clk=Clock(freq=1.0), blksize=4)
        with pytest.raises(TypeError, match="tx side of RFSampIF"):
            iface.bind("tx", RFSampIFRx(name="wrong", sim=sim))
        with pytest.raises(KeyError):
            iface.bind("master", RFSampIFTx(name="also_wrong", sim=sim))

    def test_an_unbound_interface_does_not_run_its_metronome(self):
        sim = Simulation()
        RFSampIF(name="if", sim=sim, samp_clk=Clock(freq=1.0), blksize=4, n_blk=2)
        with pytest.raises(RuntimeError, match="not fully bound"):
            sim.run_sim()

    def test_a_clockless_interface_is_refused_at_construction(self):
        sim = Simulation()
        with pytest.raises(ValueError, match="samp_clk"):
            RFSampIF(name="if", sim=sim, blksize=4)
