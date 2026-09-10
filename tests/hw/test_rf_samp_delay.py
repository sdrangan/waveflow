"""``RfSampDelay`` — the **path** between two converter edges.

``plans/rf_shot_absolute.md`` S3.  ``rf_sample_if``'s own module docstring reserves this job for a
``Channel`` block and states the bar: *"if the edge can only record a quantity and never apply it, it
does not belong on the edge."*  This is the node that applies it.

What is on trial here is the node in isolation — the shift, the tail across a block boundary, and the
two geometries it refuses.  ``examples/rf_shot_loopback`` is what puts it between two converters and
reads the delay off an address.
"""
from __future__ import annotations

import numpy as np
import pytest

from waveflow.hw.clock import Clock
from waveflow.hw.rf_sample_if import RFSampIF
from waveflow.simulation.rf_tb import RfSampDelay
from waveflow.simulation.simulation import Simulation
from waveflow.simulation.simobj import ProcessGen
from waveflow.hw.hw_module import HwModule
from waveflow.hw.rf_sample_if import RFSampIFRx, RFSampIFTx

BLKSIZE = 8
SAMP_RATE = 8e6


class _Src(HwModule):
    """A source that puts a fixed list of blocks and stops."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.rf_ep = RFSampIFTx(sim=self.sim)
        self.add_endpoint(self.rf_ep)
        self.blocks: list[np.ndarray] = []

    def run_proc(self) -> ProcessGen[None]:
        for b in self.blocks:
            yield from self.rf_ep.put(b)


class _Snk(HwModule):
    """A sink that keeps everything it is given."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.rf_ep = RFSampIFRx(sim=self.sim, depth=64)
        self.add_endpoint(self.rf_ep)
        self.got: list[np.ndarray] = []

    def run_proc(self) -> ProcessGen[None]:
        while True:
            blk = yield from self.rf_ep.get()
            self.got.append(np.array(blk.data, copy=True))


def _run(delay_samp: int, n_blk: int = 6, n_ch: int = 1):
    """Push a ramp through the node and return ``(delay node, flat output samples)``."""
    sim = Simulation()
    clk = Clock(name="samp", freq=SAMP_RATE)
    src, snk = _Src(sim=sim, name="src"), _Snk(sim=sim, name="snk")
    chan = RfSampDelay(sim=sim, name="chan", delay_samp=int(delay_samp))
    a = RFSampIF(name="in_if", sim=sim, samp_clk=clk, n_ch=n_ch, blksize=BLKSIZE, n_blk=n_blk)
    a.bind("tx", src.rf_ep)
    a.bind("rx", chan.rf_in)
    b = RFSampIF(name="out_if", sim=sim, samp_clk=clk, n_ch=n_ch, blksize=BLKSIZE, n_blk=n_blk)
    b.bind("tx", chan.rf_out)
    b.bind("rx", snk.rf_ep)
    # THE OUTPUT GRID STARTS ONE BLOCK LATE, and that is the harness rather than the node.  A block
    # exists at its grid tick and is transmitted across the FOLLOWING period, so two grids sharing an
    # epoch make the downstream one underrun on its first tick and zero-fill -- which would put zeros
    # in the capture that the path did not write.  What is on trial here is the SHIFT, so the harness
    # removes the structural hop; `examples/rf_shot_loopback` is where it is measured instead.
    b.set_t0(BLKSIZE / SAMP_RATE, owner=chan)
    # A ramp starting at 1, so a zero in the output is unambiguously the node's own fill.
    src.blocks = [np.arange(1 + i * BLKSIZE, 1 + (i + 1) * BLKSIZE, dtype=float)
                  .reshape(1, -1).repeat(n_ch, axis=0) for i in range(n_blk)]

    for o in sim._sim_objs:
        o.pre_sim()
    for o in sim._sim_objs:
        p = o.run_proc()
        if p is not None:
            sim.env.process(p)
    sim.env.run(until=(n_blk + 3) * BLKSIZE / SAMP_RATE)
    for o in sim._sim_objs:
        o.post_sim()
    flat = (np.concatenate([g[0] for g in snk.got]) if snk.got else np.zeros(0))
    return chan, flat


@pytest.mark.parametrize("delay", [0, 1, 3, BLKSIZE, BLKSIZE + 3, 2 * BLKSIZE])
def test_the_path_shifts_the_sample_stream_by_exactly_delay_samp(delay):
    """The stream out is the stream in, shifted — **in samples, not in blocks**.

    A whole-block queue could only express multiples of ``blksize``, and a demonstration whose delay
    is always a whole block cannot show that an address reading is sample-granular.  So the delays
    swept here are deliberately not all multiples of one: the tail carrying a remainder across the
    block boundary is the mechanism, and it is what these cases exercise.
    """
    chan, out = _run(delay)
    want = np.concatenate([np.zeros(delay), np.arange(1, out.size + 1, dtype=float)])[:out.size]
    assert out.size, "nothing came out of the path at all"
    assert np.array_equal(out, want), (
        f"delay {delay}: the shifted stream is wrong at sample "
        f"{int(np.flatnonzero(out != want)[0])}")
    chan.assert_ran()


def test_the_leading_silence_is_the_paths_own_fill():
    """The first ``delay_samp`` samples out are **zero**, and that is a value the path writes.

    A path with a delay in it delivers nothing for that long, and modelling it as an index shift
    instead would hide the transient the receiver actually sees — which is the one thing that still
    distinguishes two delays that alias in the address.
    """
    _chan, out = _run(5)
    assert np.array_equal(out[:5], np.zeros(5))
    assert out[5] == 1.0, "the first real sample is not the source's first sample"


def test_a_path_that_reblocks_is_refused():
    """The two edges must agree about what a block is — a path delays samples, it does not re-block.

    Refused in ``pre_sim`` rather than silently reshaped: a node that quietly changed ``blksize``
    would make every downstream index mean something different, and nothing else in the graph would
    say so.
    """
    sim = Simulation()
    clk = Clock(name="samp", freq=SAMP_RATE)
    src, snk = _Src(sim=sim, name="src"), _Snk(sim=sim, name="snk")
    chan = RfSampDelay(sim=sim, name="chan", delay_samp=1)
    a = RFSampIF(name="in_if", sim=sim, samp_clk=clk, n_ch=1, blksize=BLKSIZE, n_blk=2)
    a.bind("tx", src.rf_ep)
    a.bind("rx", chan.rf_in)
    b = RFSampIF(name="out_if", sim=sim, samp_clk=clk, n_ch=1, blksize=2 * BLKSIZE, n_blk=2)
    b.bind("tx", chan.rf_out)
    b.bind("rx", snk.rf_ep)
    with pytest.raises(ValueError, match="does not re-block"):
        chan.pre_sim()


def test_a_negative_delay_is_refused():
    """A path cannot deliver a sample before it was sent."""
    sim = Simulation()
    clk = Clock(name="samp", freq=SAMP_RATE)
    src, snk = _Src(sim=sim, name="src"), _Snk(sim=sim, name="snk")
    chan = RfSampDelay(sim=sim, name="chan", delay_samp=-1)
    a = RFSampIF(name="in_if", sim=sim, samp_clk=clk, n_ch=1, blksize=BLKSIZE, n_blk=2)
    a.bind("tx", src.rf_ep)
    a.bind("rx", chan.rf_in)
    b = RFSampIF(name="out_if", sim=sim, samp_clk=clk, n_ch=1, blksize=BLKSIZE, n_blk=2)
    b.bind("tx", chan.rf_out)
    b.bind("rx", snk.rf_ep)
    with pytest.raises(ValueError, match="before it was sent"):
        chan.pre_sim()


def test_the_path_carries_every_channel_of_a_block():
    """All of a tile's channels ride one block, so the shift applies to every row of it.

    The delay is a **bulk** one — a property of the path, one number — and per-channel skew is
    explicitly not this node's business: ``RFSampIF.t0``'s docstring records that a vector there was
    recordable and never applied, which is worse than absent.
    """
    chan, flat = _run(3, n_ch=3)
    assert chan.n_in == chan.n_out
    assert flat.size and np.array_equal(flat[:3], np.zeros(3))


def test_the_path_declares_a_block_latency_of_zero():
    """Declared rather than merely true, because a graph has to be able to **sum** it.

    ``examples/rf_shot_loopback`` adds this to the converter's own hop to get the loop's structural
    latency, exactly as ``examples/rf_loopback`` sums ``RfSampPassThrough.blk_latency``.  A zero that
    is stated can be added up; one that is only a fact about the body cannot.
    """
    chan = RfSampDelay(sim=Simulation(), name="chan", delay_samp=7)
    assert chan.blk_latency == 0
    assert chan.delay_samp == 7


def test_a_path_nobody_fed_says_so():
    """The reachability guard: a delay of zero and a path nobody fed look identical downstream."""
    chan = RfSampDelay(sim=Simulation(), name="chan", delay_samp=1)
    with pytest.raises(AssertionError, match="never handed a block"):
        chan.assert_ran()
