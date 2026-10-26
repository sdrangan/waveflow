"""Steps 11-12 of plans/sionna_rt/sionna_rt.md: the RF environment's nodes on ``RFSampIF`` edges.

Every ``RtChannel`` test is named ``test_channel_*`` and every ``RtSignalSource`` test
``test_source_*``, so the plan's selectors (``-k test_channel_``, ``-k test_source_``) cannot miss
one (M4 review).  The graphs are in ``tests/rfenv/_graphs.py``.
"""

from __future__ import annotations

import numpy as np
import pytest

from tests.rfenv._graphs import (
    BLK,
    FS,
    IQ_WORD,
    channel_graph,
    play,
    rel_err,
    run,
    signal,
    source_graph,
)
from tests.rfenv._pathsets import pathset, random_pathset
from tests.rfenv._reference import DATA
from waveflow.hw.clock import Clock
from waveflow.hw.hw_module import discover_dyn_params
from waveflow.hw.interface import StreamIF
from waveflow.hw.rf_sample_if import RFSampIF
from waveflow.hw.rfdc import Rfdc
from waveflow.rfenv.apply import ChannelApplier, reference_apply
from waveflow.rfenv.nodes import RtChannel, RtSignalSource, resolve_channel
from waveflow.rfenv.noise import (
    amplitude_gain,
    dbm_to_w,
    gain_db_for_dbfs,
    thermal_noise_power,
)
from waveflow.rfenv.paths import PathSet
from waveflow.rfenv.sionna_backend import DeviceSpec, SceneSpec
from waveflow.rfenv.waveforms import ArrayWaveform, Tone, ZadoffChu
from waveflow.simulation.rf_tb import read_rf_bundle
from waveflow.simulation.simulation import Simulation
from waveflow.simulation.stream_tb import StreamSink

SPEC = SceneSpec(
    scene="empty",
    frequency_hz=3.5e9,
    transmitters=(DeviceSpec("tx", (0.0, 0.0, 1.0)),),
    receivers=(DeviceSpec("rx", (1.0, 0.0, 1.0)),),
)


def first_blocks(src: RtSignalSource, n: int) -> np.ndarray:
    return np.concatenate(
        [b for b, _ in zip(src.blocks(BLK, FS), range(n), strict=False)], axis=1
    )


# --- RtChannel (step 11) ----------------------------------------------------------------------


@pytest.mark.parametrize(("n_rx", "n_tx"), [(1, 1), (4, 2)])
def test_channel_applies_the_channel_block_by_block(n_rx, n_tx):
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
    assert chan.dropped_blocks == 0 and chan.latency_samp == 32


def test_channel_gains_scale_amplitude():
    ps = pathset([[[1e-3]]], [[[1e-7]]])
    x = signal(1, 6, seed=23)
    _, _, out, _, _ = play(x, 6, 1, pathset=ps, tx_gain_db=20.0, gain_db=40.0)
    want = reference_apply(ps, x, FS) * amplitude_gain(60.0)
    assert rel_err(out, want) <= 1e-12


def test_channel_thermal_noise_adds_ktbf_at_the_antenna():
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


def test_channel_a_dropped_block_is_replaced_and_the_output_stays_on_its_grid():
    """Blocks 1 and 3 arrive (2 was dropped at the receive queue): the node emits three blocks, the
    middle one the channel's response to zero input, and counts the gap."""
    ps = random_pathset(1, 1, 4, seed=27, max_tau=5e-7)
    x = signal(1, 3, seed=28)
    chan = RtChannel(sim=Simulation(), name="gap", pathset=ps)
    chan.paths = ps
    chan._applier = ChannelApplier(ps, FS, start_n0=0)
    out = chan.step(1, x[:, :BLK]) + chan.step(3, x[:, 2 * BLK :])
    assert len(out) == 3 and chan.dropped_blocks == 1
    x_gap = x.copy()
    x_gap[:, BLK : 2 * BLK] = 0
    np.testing.assert_allclose(
        np.concatenate(out, axis=1), reference_apply(ps, x_gap, FS), rtol=0, atol=1e-15
    )


def test_channel_a_committed_file_runs_the_node():
    x = signal(1, 8, seed=24)
    chan, _, out, _, _ = play(x, 8, 1, channel=DATA / "floor_wall_siso")
    assert rel_err(out, reference_apply(chan.paths, x, FS)) <= 1e-12


def test_channel_a_relative_file_resolves_against_channel_root():
    x = signal(1, 4, seed=25)
    chan, _, _, _, _ = play(x, 4, 1, channel="floor_wall_siso", channel_root=DATA)
    assert chan.paths.n_paths == 2


@pytest.mark.parametrize(
    ("n_in", "n_out", "match"),
    [(1, 2, "rf_out 2 channels"), (2, 1, "rf_in carries 2")],
)
def test_channel_a_count_that_disagrees_is_refused(n_in, n_out, match):
    chan = channel_graph(n_in, n_out, 2, pathset=random_pathset(1, 1, 2, 1))[2]
    with pytest.raises(ValueError, match=match):
        chan.pre_sim()


def test_channel_edges_that_disagree_are_refused_before_any_trace():
    """A wrongly wired edge is refused before the channel is resolved: here the spec is live and
    would need a trace, but the edge check fires first."""
    sim = Simulation()
    chan = RtChannel(sim=sim, name="chan", spec=SPEC, mode="live")
    a = RFSampIF(name="a", sim=sim, samp_clk=Clock(name="c", freq=FS), blksize=BLK,
                 complex_samp=True)  # fmt: skip
    b = RFSampIF(name="b", sim=sim, samp_clk=Clock(name="c2", freq=FS), blksize=2 * BLK,
                 complex_samp=True)  # fmt: skip
    a.bind("rx", chan.rf_in)
    b.bind("tx", chan.rf_out)
    with pytest.raises(ValueError, match="re-blocks"):
        chan.pre_sim()
    chan2 = RtChannel(sim=sim, name="chan2", pathset=random_pathset(1, 1, 2, seed=1))
    a2 = RFSampIF(name="a2", sim=sim, samp_clk=Clock(name="c4", freq=FS), blksize=BLK,
                  complex_samp=True)  # fmt: skip
    b2 = RFSampIF(name="b2", sim=sim, samp_clk=Clock(name="c3", freq=FS), blksize=BLK)
    a2.bind("rx", chan2.rf_in)
    b2.bind("tx", chan2.rf_out)
    with pytest.raises(ValueError, match="complex baseband"):
        chan2.pre_sim()


def test_channel_an_idle_node_says_so():
    chan = RtChannel(
        sim=Simulation(), name="idle", pathset=random_pathset(1, 1, 2, seed=1)
    )
    with pytest.raises(AssertionError, match="never handed a block"):
        chan.assert_ran()


@pytest.mark.parametrize(
    ("kw", "match"),
    [
        ({"pathset": random_pathset(1, 1, 1, seed=1), "spec": SPEC}, "not both"),
        (
            {"pathset": random_pathset(1, 1, 1, seed=1), "mode": "live"},
            "already traced",
        ),
        ({"spec": SPEC, "channel": "x", "mode": "live"}, "reads no channel file"),
        ({"mode": "live"}, "needs a spec"),
        ({"spec": SPEC}, "channel file stem"),
        ({}, "no channel"),
        ({"mode": "warm"}, "mode must be"),
    ],
)
def test_channel_the_source_of_a_channel_is_exactly_one(kw, match):
    args = {"pathset": None, "channel": None, "spec": None, "mode": "cached"} | kw
    with pytest.raises(ValueError, match=match):
        resolve_channel(who="t", channel_root=None, **args)


def test_channel_retrace_never_protects_a_committed_file():
    """A spec pointed at a committed file with retrace='never' never traces: a spec the file was
    not traced from is refused rather than traced over it."""
    with pytest.raises(ValueError, match="retrace='never'"):
        resolve_channel(
            who="t",
            pathset=None,
            channel=DATA / "floor_wall_siso",
            spec=SPEC,
            mode="cached",
            channel_root=None,
            retrace="never",
        )


# --- RtSignalSource (step 12) -----------------------------------------------------------------


def test_source_plays_what_it_renders(tmp_path):
    """The pysim blocks equal the bundle the XSI twin will replay, bit for bit."""
    kw = {
        "channel": DATA / "floor_wall_siso",
        "waveform": Tone(freq_hz=1.0e6),
        "tx_power_dbm": 30.0,
        "gain_db": 60.0,
        "noise_figure_db": 7.0,
        "seed": 3,
    }
    sim, src, snk, e = source_graph(1, 8, **kw)
    run(sim, 10)
    played = np.concatenate(snk.got, axis=1)
    bundle = src.render_bundle(tmp_path / "rf")  # the edge's geometry and n_blk
    rendered = np.concatenate(read_rf_bundle(bundle, 1, BLK, complex_samp=True), axis=1)
    assert played.shape == rendered.shape == (1, 8 * BLK)
    np.testing.assert_array_equal(played, rendered)
    e.assert_clean()


def test_source_renders_only_the_edges_geometry(tmp_path):
    src = source_graph(1, 4, pathset=random_pathset(1, 1, 1, seed=1))[1]
    with pytest.raises(ValueError, match="render at"):
        src.render_bundle(tmp_path / "rf", samp_rate=128e6)
    unbound = RtSignalSource(
        sim=Simulation(), name="u", pathset=random_pathset(1, 1, 1, 1)
    )
    with pytest.raises(ValueError, match="needs blksize and samp_rate"):
        unbound.render_bundle(tmp_path / "rf2", 2)
    assert unbound.render_bundle(
        tmp_path / "rf3", 2, blksize=BLK, samp_rate=FS
    ).is_dir()


def test_source_is_the_channel_applied_to_the_waveform():
    ps = random_pathset(2, 1, 4, seed=26)
    w = ZadoffChu(length=139, root=5)
    sim, _, snk, _ = source_graph(2, 6, pathset=ps, waveform=w, tx_power_dbm=10.0)
    run(sim, 8)
    x = np.sqrt(dbm_to_w(10.0)) * w.samples(0, 6 * BLK, FS)[None, :]
    want = reference_apply(ps, x, FS)
    assert rel_err(np.concatenate(snk.got, axis=1), want) <= 1e-12


def test_source_gain_scales_amplitude_and_noise_is_ktbf():
    """Checked against the source itself, not only its bundle: 20 dB more gain is 10x the
    amplitude, and with no signal the output is kTB·F times the gain squared."""
    ps = random_pathset(1, 1, 3, seed=29)

    def src(**kw):
        return RtSignalSource(sim=Simulation(), name="s", pathset=ps, **kw)

    a = first_blocks(src(tx_power_dbm=10.0), 4)
    b = first_blocks(src(tx_power_dbm=10.0, gain_db=20.0), 4)
    assert rel_err(b, 10.0 * a) <= 1e-12
    n = first_blocks(src(tx_power_dbm=-300.0, noise_figure_db=5.0, gain_db=80.0), 400)
    want = thermal_noise_power(FS, 5.0) * amplitude_gain(80.0) ** 2
    assert abs(np.mean(np.abs(n) ** 2) / want - 1.0) < 0.05


def test_source_feeds_an_adc_cleanly():
    """AC6, pysim part: 64 blocks into an Rfdc ADC with the I/Q word, the edge clean, and every
    word the ADC emits exactly the converter's own quantization of the source's samples.
    """
    n_blk = 64
    ps = PathSet.load(DATA / "floor_wall_siso")
    p_rx = float(ps.power_gain().sum()) * dbm_to_w(30.0)
    gain = gain_db_for_dbfs(p_rx, target_dbfs=-10.0, full_scale=1.0)
    sim = Simulation()
    samp_clk, axis_clk = Clock(name="samp", freq=FS), Clock(name="axis", freq=250e6)
    src = RtSignalSource(sim=sim, name="src", pathset=ps, waveform=Tone(freq_hz=2.0e6),
                         tx_power_dbm=30.0, gain_db=gain, n_blk=n_blk)  # fmt: skip
    rfdc = Rfdc(name="rfdc", sim=sim, word=IQ_WORD, n_rx=1, n_tx=0, full_scale=1.0)
    snk = StreamSink(
        name="axis_snk", sim=sim, bitwidth=rfdc.axis_bitwidth, has_tlast=True
    )
    adc = RFSampIF(name="adc_if", sim=sim, samp_clk=samp_clk, n_ch=1, blksize=BLK, n_blk=n_blk,
                   complex_samp=True)  # fmt: skip
    adc.bind("tx", src.rf_ep)
    adc.bind("rx", rfdc.rx_rf)
    axis = StreamIF(name="axis", sim=sim, clk=axis_clk, bitwidth=rfdc.axis_bitwidth)
    axis.bind("master", rfdc.rx_streams[0])
    axis.bind("slave", snk.stream_ep)
    run(sim, n_blk + 4)
    adc.assert_clean()
    words = np.concatenate([np.asarray(w).reshape(-1) for w in snk.words])
    want = first_blocks(src, n_blk)
    assert words.size == n_blk * BLK // IQ_WORD.samp_per_word
    np.testing.assert_array_equal(words, np.asarray(rfdc._pack(want / 1.0)).reshape(-1))


def test_source_rule_13_in_bundle_is_the_only_dyn_param():
    src = RtSignalSource(sim=Simulation(), name="src", in_bundle="vectors/rt_in",
                         pathset=random_pathset(1, 1, 1, seed=1), tx_power_dbm=5.0)  # fmt: skip
    assert discover_dyn_params(src) == {"in_bundle": "vectors/rt_in"}


def test_source_xsi_twin_is_rf_file_source():
    src = source_graph(2, 2, pathset=random_pathset(2, 1, 1, seed=1), in_bundle="v/rt")[
        1
    ]
    m = src.bfm_model()
    assert m.cls == "RfFileSource" and m.ports == ("rf_ep",)
    assert m.extra_args == (str(2 * BLK * 2), "1")


def test_source_xsi_twin_needs_a_bundle():
    src = source_graph(1, 2, pathset=random_pathset(1, 1, 1, seed=1))[1]
    with pytest.raises(ValueError, match="set in_bundle"):
        src.bfm_model()


def test_source_a_waveform_with_the_wrong_channel_count_is_refused():
    src = RtSignalSource(sim=Simulation(), name="src", pathset=random_pathset(1, 2, 1, 1),
                         waveform=ArrayWaveform(data=np.ones((3, 4))))  # fmt: skip
    with pytest.raises(ValueError, match="3 channels"):
        next(src.blocks(BLK, FS))


def test_source_a_waveform_with_the_wrong_channel_count_is_refused_at_pre_sim():
    """The waveform's declared channel count is checked against the channel before the run."""
    _, src, _, _ = source_graph(
        1,
        2,
        pathset=random_pathset(1, 2, 1, 1),
        waveform=ArrayWaveform(data=np.ones((3, 4))),
    )
    with pytest.raises(ValueError, match="3 transmit channels"):
        src.pre_sim()


def test_source_pre_sim_applies_the_current_channel(tmp_path):
    """A channel changed after a render is the one the run applies, as for RtChannel."""
    first, second = random_pathset(1, 1, 2, 1), random_pathset(1, 1, 2, 2)
    _, src, _, _ = source_graph(1, 2, pathset=first)
    src.render_bundle(tmp_path / "rf")
    assert src.paths is first
    src.pathset = second
    src.pre_sim()
    assert src.paths is second


def test_source_an_edge_that_disagrees_is_refused():
    src = source_graph(3, 2, pathset=random_pathset(2, 1, 1, seed=1))[1]
    with pytest.raises(ValueError, match="2 receive channels"):
        src.pre_sim()
