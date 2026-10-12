"""Steps 11-12 of plans/sionna_rt/sionna_rt.md: the RF environment's nodes on ``RFSampIF`` edges.

The harness is ``tests/hw/test_rf_samp_delay.py``'s: a list-of-blocks source, a keep-everything sink,
and the output grid started one block late, because a block is transmitted across the period after
its tick -- the structural hop, which is the harness's and not the node's.
"""

from __future__ import annotations

import numpy as np
import pytest

from tests.rfenv._pathsets import pathset, random_pathset
from tests.rfenv._reference import DATA
from waveflow.hw.clock import Clock
from waveflow.hw.hw_module import HwModule
from waveflow.hw.rf_sample_if import RFSampIF, RFSampIFRx, RFSampIFTx
from waveflow.rfenv.apply import reference_apply
from waveflow.rfenv.nodes import RtChannel, resolve_channel
from waveflow.rfenv.noise import amplitude_gain, thermal_noise_power
from waveflow.rfenv.sionna_backend import DeviceSpec, SceneSpec
from waveflow.simulation.simobj import ProcessGen
from waveflow.simulation.simulation import Simulation

BLK = 64
FS = 122.88e6


class _Src(HwModule):
    def __post_init__(self) -> None:
        super().__post_init__()
        self.rf_ep = RFSampIFTx(sim=self.sim)
        self.add_endpoint(self.rf_ep)
        self.blocks: list[np.ndarray] = []

    def run_proc(self) -> ProcessGen[None]:
        for b in self.blocks:
            yield from self.rf_ep.put(b)


class _Snk(HwModule):
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
    """``_Src -> RFSampIF -> RtChannel -> RFSampIF -> _Snk``; returns (sim, src, chan, snk, a, b)."""
    sim = Simulation()
    clk = Clock(name="samp", freq=FS)
    src, snk = _Src(sim=sim, name="src"), _Snk(sim=sim, name="snk")
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


def signal(n_ch: int, n_blk: int, seed: int) -> np.ndarray:
    rng = np.random.default_rng(seed)
    shape = (n_ch, n_blk * BLK)
    return (rng.standard_normal(shape) + 1j * rng.standard_normal(shape)) * 0.1


def play(x: np.ndarray, n_blk: int, n_out: int, **chan_kw):
    sim, src, chan, snk, a, b = channel_graph(x.shape[0], n_out, n_blk, **chan_kw)
    src.blocks = [x[:, i * BLK : (i + 1) * BLK] for i in range(n_blk)]
    run(sim, n_blk + 3)
    out = np.concatenate(snk.got, axis=1) if snk.got else np.zeros((n_out, 0))
    return chan, snk, out, a, b


def rel_err(got, want) -> float:
    return float(np.linalg.norm(got - want) / np.linalg.norm(want))


# --- RtChannel (step 11) ----------------------------------------------------------------------


@pytest.mark.parametrize(("n_rx", "n_tx"), [(1, 1), (4, 2)])
def test_the_channel_node_applies_the_channel_block_by_block(n_rx, n_tx):
    ps = random_pathset(n_rx, n_tx, 5, seed=21)
    n_blk = 12
    x = signal(n_tx, n_blk, seed=22)
    chan, snk, out, a, b = play(x, n_blk, n_rx, pathset=ps)
    want = reference_apply(ps, x, FS)
    assert out.shape[1] == n_blk * BLK and snk.idx == list(range(1, n_blk + 1))
    assert rel_err(out, want) <= 1e-12
    a.assert_clean()
    b.assert_clean()
    chan.assert_ran()
    assert chan.dropped_samples == 0 and chan.latency_samp == 32


def test_the_gains_scale_amplitude():
    ps = pathset([[[1e-3]]], [[[1e-7]]])
    x = signal(1, 6, seed=23)
    _, _, out, _, _ = play(x, 6, 1, pathset=ps, tx_gain_db=20.0, gain_db=40.0)
    want = reference_apply(ps, x, FS) * amplitude_gain(60.0)
    assert rel_err(out, want) <= 1e-12


def test_thermal_noise_adds_ktbf_at_the_antenna():
    """With no paths at all, what comes out is the noise: kTB·F, times the receive gain squared."""
    ps = pathset([[[0.0]]], [[[-1.0]]], valid=[[[False]]])
    n_blk = 400
    _, _, out, _, _ = play(
        np.zeros((1, n_blk * BLK)),
        n_blk,
        1,
        pathset=ps,
        noise_figure_db=7.0,
        gain_db=60.0,
    )
    want = thermal_noise_power(FS, 7.0) * amplitude_gain(60.0) ** 2
    assert abs(np.mean(np.abs(out) ** 2) / want - 1.0) < 0.05


def test_a_committed_channel_file_runs_the_node():
    x = signal(1, 8, seed=24)
    chan, _, out, _, _ = play(x, 8, 1, channel=DATA / "floor_wall_siso")
    assert rel_err(out, reference_apply(chan.paths, x, FS)) <= 1e-12


def test_a_relative_channel_file_resolves_against_root():
    x = signal(1, 4, seed=25)
    chan, _, _, _, _ = play(x, 4, 1, channel="floor_wall_siso", root=DATA)
    assert chan.paths.n_paths == 2


@pytest.mark.parametrize(
    ("n_in", "n_out", "match"),
    [(1, 2, "rf_out 2 channels"), (2, 1, "rf_in carries 2")],
)
def test_a_channel_count_that_disagrees_is_refused(n_in, n_out, match):
    chan = channel_graph(n_in, n_out, 2, pathset=random_pathset(1, 1, 2, 1))[2]
    with pytest.raises(ValueError, match=match):
        chan.pre_sim()


def test_edges_that_disagree_are_refused():
    sim = Simulation()
    chan = RtChannel(sim=sim, name="chan", pathset=random_pathset(1, 1, 2, seed=1))
    a = RFSampIF(name="a", sim=sim, samp_clk=Clock(name="c", freq=FS), blksize=BLK,
                 complex_samp=True)  # fmt: skip
    b = RFSampIF(name="b", sim=sim, samp_clk=Clock(name="c2", freq=FS), blksize=2 * BLK,
                 complex_samp=True)  # fmt: skip
    a.bind("rx", chan.rf_in)
    b.bind("tx", chan.rf_out)
    with pytest.raises(ValueError, match="re-blocks"):
        chan.pre_sim()
    b2 = RFSampIF(name="b2", sim=sim, samp_clk=Clock(name="c3", freq=FS), blksize=BLK)
    chan2 = RtChannel(sim=sim, name="chan2", pathset=random_pathset(1, 1, 2, seed=1))
    a2 = RFSampIF(name="a2", sim=sim, samp_clk=Clock(name="c4", freq=FS), blksize=BLK,
                  complex_samp=True)  # fmt: skip
    a2.bind("rx", chan2.rf_in)
    b2.bind("tx", chan2.rf_out)
    with pytest.raises(ValueError, match="complex baseband"):
        chan2.pre_sim()


def test_an_idle_channel_says_so():
    chan = RtChannel(
        sim=Simulation(), name="idle", pathset=random_pathset(1, 1, 2, seed=1)
    )
    with pytest.raises(AssertionError, match="never handed a block"):
        chan.assert_ran()


SPEC = SceneSpec(
    scene="empty",
    frequency_hz=3.5e9,
    transmitters=(DeviceSpec("tx", (0.0, 0.0, 1.0)),),
    receivers=(DeviceSpec("rx", (1.0, 0.0, 1.0)),),
)


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({"pathset": random_pathset(1, 1, 1, seed=1), "spec": SPEC}, "not both"),
        ({"mode": "live"}, "needs a spec"),
        ({"spec": SPEC}, "channel file stem"),
        ({}, "no channel"),
        ({"mode": "warm"}, "mode must be"),
    ],
)
def test_the_channel_source_is_exactly_one(kw, match):
    args = {
        "pathset": None,
        "channel": None,
        "spec": None,
        "mode": "cached",
        "root": None,
    } | kw
    with pytest.raises(ValueError, match=match):
        resolve_channel(who="t", **args)
