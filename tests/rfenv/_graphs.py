"""Test graphs for the RF environment's nodes (``tests/rfenv/test_nodes.py``, ``test_live.py``).

The harness is ``tests/hw/test_rf_samp_delay.py``'s: a list-of-blocks source, a keep-everything sink,
and the output grid started one block late, because a block is transmitted across the period after
its tick -- the structural hop, which is the harness's and not the node's.
"""

from __future__ import annotations

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.hw_module import HwModule
from waveflow.hw.rf_sample_if import RFSampIF, RFSampIFRx, RFSampIFTx
from waveflow.hw.rfdc_samp_word import Rfsoc4x2SampWord
from waveflow.rfenv.nodes import RtChannel, RtSignalSource
from waveflow.simulation.simobj import ProcessGen
from waveflow.simulation.simulation import Simulation

BLK = 64
FS = 122.88e6
#: The 4x2's I/Q word: 2 complex samples a 64-bit beat (examples/rf_loopback/rf_loopback_xsi.py).
IQ_WORD = Rfsoc4x2SampWord.specialize(samp_per_word=2, iq_mode=True)


class Src(HwModule):
    """Puts a fixed list of blocks, then stops."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.rf_ep = RFSampIFTx(sim=self.sim)
        self.add_endpoint(self.rf_ep)
        self.blocks: list[np.ndarray] = []

    def run_proc(self) -> ProcessGen[None]:
        for b in self.blocks:
            yield from self.rf_ep.put(b)


class Snk(HwModule):
    """Keeps every block it is given, with its grid index."""

    def __post_init__(self) -> None:
        super().__post_init__()
        self.rf_ep = RFSampIFRx(sim=self.sim, depth=64)
        self.add_endpoint(self.rf_ep)
        self.got: list[np.ndarray] = []
        self.idx: list[int] = []

    def run_proc(self) -> ProcessGen[None]:
        while True:
            blk = yield from self.rf_ep.get()
            self.got.append(np.array(blk.data, copy=True))
            self.idx.append(int(blk.idx))


def run(sim: Simulation, horizon_blocks: int) -> None:
    """pre_sim, run_proc, a horizon in block periods, post_sim."""
    for o in sim._sim_objs:
        o.pre_sim()
    for o in sim._sim_objs:
        p = o.run_proc()
        if p is not None:
            sim.env.process(p)
    sim.env.run(until=horizon_blocks * BLK / FS)
    for o in sim._sim_objs:
        o.post_sim()


def channel_graph(n_in: int, n_out: int, n_blk: int, **chan_kw):
    """``Src -> RFSampIF -> RtChannel -> RFSampIF -> Snk``; returns (sim, src, chan, snk, a, b)."""
    sim = Simulation()
    clk = Clock(name="samp", freq=FS)
    src, snk = Src(sim=sim, name="src"), Snk(sim=sim, name="snk")
    chan = RtChannel(sim=sim, name="chan", **chan_kw)
    a = RFSampIF(name="in_if", sim=sim, samp_clk=clk, n_ch=n_in, blksize=BLK, n_blk=n_blk,
                 complex_samp=True)  # fmt: skip
    a.bind("tx", src.rf_ep)
    a.bind("rx", chan.rf_in)
    b = RFSampIF(name="out_if", sim=sim, samp_clk=clk, n_ch=n_out, blksize=BLK, n_blk=n_blk,
                 complex_samp=True)  # fmt: skip
    b.bind("tx", chan.rf_out)
    b.bind("rx", snk.rf_ep)
    b.set_t0(BLK / FS, owner=chan)
    return sim, src, chan, snk, a, b


def source_graph(n_ch: int, n_blk: int, **src_kw):
    """``RtSignalSource -> RFSampIF -> Snk``; returns (sim, src, snk, edge)."""
    sim = Simulation()
    clk = Clock(name="samp", freq=FS)
    src = RtSignalSource(sim=sim, name="src", n_blk=n_blk, **src_kw)
    snk = Snk(sim=sim, name="snk")
    e = RFSampIF(name="edge", sim=sim, samp_clk=clk, n_ch=n_ch, blksize=BLK, n_blk=n_blk,
                 complex_samp=True)  # fmt: skip
    e.bind("tx", src.rf_ep)
    e.bind("rx", snk.rf_ep)
    return sim, src, snk, e


def signal(n_ch: int, n_blk: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    shape = (n_ch, n_blk * BLK)
    return (rng.standard_normal(shape) + 1j * rng.standard_normal(shape)) * 0.1


def play(x: np.ndarray, n_blk: int, n_out: int, **chan_kw):
    """Run ``x`` through a channel graph; returns (chan, snk, output, in edge, out edge)."""
    sim, src, chan, snk, a, b = channel_graph(x.shape[0], n_out, n_blk, **chan_kw)
    src.blocks = [x[:, i * BLK : (i + 1) * BLK] for i in range(n_blk)]
    run(sim, n_blk + 3)
    out = np.concatenate(snk.got, axis=1) if snk.got else np.zeros((n_out, 0))
    return chan, snk, out, a, b


def rel_err(got, want) -> float:
    return float(np.linalg.norm(got - want) / np.linalg.norm(want))
