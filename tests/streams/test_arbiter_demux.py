"""StreamArbiter and StreamDemux (``waveflow/streams/``, ``plans/shared_kernel.md`` Stage 2).

pysim: the arbiter's policies on backlogged inputs (strict priority, round-robin), frames never split or
interleaved; the demux by ``s_sel`` and by a header field, an out-of-range index dropped and counted; the
pair ``arbiter -> demux`` returning every input on its own output.  The bodies' signatures and steps.  At
RTL (``-m xsi``): the demux alone both ways, and the pair under both policies -- the pair is the arbiter's
gate, since its interleave depends on timing and the pair's outputs do not.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from waveflow.build.build import BuildConfig
from waveflow.hw.clock import Clock
from waveflow.hw.harness import harness
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.simulation.simulation import Simulation
from waveflow.streams import StreamArbiter, StreamArbiterStep, StreamDemux, StreamDemuxStep
from waveflow.streams import rtl
from waveflow.streams.rtl import ArbiterDemuxPair
from waveflow.toolchain.toolchain import find_vitis_path, find_vivado_path

ROOT = Path(__file__).resolve().parents[2] / "work" / "streams"
PER = 1e-8


def _words(sysm, port):
    return [np.asarray(b, dtype=np.uint64) for b in sysm.stand_ins[port].words]


def _assert_golden(sysm):
    for port, want in sysm.golden_outputs().items():
        got = _words(sysm, port)
        assert len(got) == len(want), port
        for g, w in zip(got, want):
            np.testing.assert_array_equal(g, np.asarray(w, dtype=np.uint64))


# --- the arbiter on backlogged inputs: the policy, exactly ------------------------------------------


def _backlogged(policy, n=3, frames=4, frame_bursts=1, hold=2000):
    """Every input loaded with *frames* frames while ``m_out``'s reader holds off for *hold* cycles, so
    from the second grant on every input is backlogged.  Returns ``(sel, out bursts)``."""
    sim = Simulation()
    clk = Clock(freq=1 / PER)
    arb = StreamArbiter(name="arb", sim=sim, n_in=n, bitwidth=32, frame_bursts=frame_bursts, policy=policy)
    drivers = []
    for k in range(n):
        tx = StreamIFMaster(name=f"d{k}", sim=sim, bitwidth=32)
        link = StreamIF(name=f"in{k}", sim=sim, clk=clk, bitwidth=32, framed=True, depth=4096)
        link.bind("master", tx)
        link.bind("slave", arb.s_in[k])
        drivers.append(tx)
    sinks = {}
    for port in ("m_out", "m_sel"):
        rx = StreamIFSlave(name=f"r_{port}", sim=sim, bitwidth=32)
        link = StreamIF(name=f"o_{port}", sim=sim, clk=clk, bitwidth=32, framed=True,
                        depth=4 if port == "m_out" else 4096)
        link.bind("master", getattr(arb, port))
        link.bind("slave", rx)
        sinks[port] = rx
    got = {"m_out": [], "m_sel": []}

    def drive(k):
        for f in range(frames):
            for b in range(frame_bursts):
                yield from drivers[k].write(np.asarray([k << 8 | f << 4 | b] * 2, dtype=np.uint32))

    def read(port, wait):
        yield sim.env.timeout(wait * PER)
        while True:
            got[port].append((yield from sinks[port].get()))

    for k in range(n):
        sim.env.process(drive(k))
    sim.env.process(read("m_out", hold))
    sim.env.process(read("m_sel", 0))
    sim.run_sim()
    return [int(s[0]) for s in got["m_sel"]], got["m_out"]


def test_a_frame_announced_before_it_is_all_there_holds_the_others():
    """The arbiter's first rule, as at RTL (``plans/stream_dispatch.md`` Stage 8): it commits to a frame on
    its first word and copies it as the words arrive.  Input 1's frame, begun at cycle 0 and held open
    until 200, wins at its first word; input 0's, written whole at cycle 5 (and the higher priority),
    waits behind it until it is all there."""
    sim = Simulation()
    clk = Clock(freq=1 / PER)
    arb = StreamArbiter(name="arb", sim=sim, n_in=2, bitwidth=32, policy="priority", clk=clk)
    tx = []
    for k in range(2):
        t = StreamIFMaster(name=f"d{k}", sim=sim, bitwidth=32)
        link = StreamIF(name=f"in{k}", sim=sim, clk=clk, bitwidth=32, framed=True, depth=64)
        link.bind("master", t)
        link.bind("slave", arb.s_in[k])
        tx.append(t)
    got = {"m_out": [], "m_sel": []}
    rx = {}
    for port in got:
        rx[port] = StreamIFSlave(name=f"r_{port}", sim=sim, bitwidth=32)
        link = StreamIF(name=f"o_{port}", sim=sim, clk=clk, bitwidth=32, framed=True, depth=64)
        link.bind("master", getattr(arb, port))
        link.bind("slave", rx[port])

    def slow():
        x = yield from tx[1].begin(np.asarray([0x10, 0x11, 0x12], dtype=np.uint32))
        yield sim.env.timeout(200 * PER)
        yield from tx[1].end(x)

    def fast():
        yield sim.env.timeout(5 * PER)
        yield from tx[0].write(np.asarray([0x00, 0x01], dtype=np.uint32))

    def read(port):
        while True:
            words = yield from rx[port].get()
            got[port].append((round(sim.env.now / PER), int(words[0])))

    for p in (slow(), fast(), read("m_out"), read("m_sel")):
        sim.env.process(p)
    sim.run_sim()
    assert [w for _, w in got["m_sel"]] == [1, 0]
    assert [w for _, w in got["m_out"]] == [0x10, 0x00]
    assert got["m_out"][0][0] >= 200 and got["m_out"][1][0] > got["m_out"][0][0]


@pytest.mark.parametrize("route", ["sel", "header"])
def test_the_demux_commits_on_a_first_word_and_copies_as_words_arrive(route):
    """As its body (``plans/osfb_pysim_fidelity.md``, cause H): a frame begun at cycle 0 and held open until
    200 is routed at its first word and its output starts then -- begun on the output's link long before
    it is all there; the next frame, for another output and written whole at cycle 5, waits behind it."""
    sim = Simulation()
    clk = Clock(freq=1 / PER)
    dmx = StreamDemux(name="dmx", sim=sim, n_out=2, bitwidth=32, route=route, sel_lsb=16, sel_bits=8, clk=clk)
    tx = {}
    for port in ("s_in", "s_sel") if route == "sel" else ("s_in",):
        tx[port] = StreamIFMaster(name=f"d_{port}", sim=sim, bitwidth=32)
        link = StreamIF(name=f"in_{port}", sim=sim, clk=clk, bitwidth=32, framed=True, depth=64)
        link.bind("master", tx[port])
        link.bind("slave", getattr(dmx, port))
    rx, got = [], []
    for k in range(2):
        r = StreamIFSlave(name=f"r{k}", sim=sim, bitwidth=32)
        link = StreamIF(name=f"o{k}", sim=sim, clk=clk, bitwidth=32, framed=True, depth=64)
        link.bind("master", dmx.m_out[k])
        link.bind("slave", r)
        rx.append(r)
    seen = {}

    def frame(k, f):
        w = [f << 24 | j for j in range(3)]
        return np.asarray([w[0] | (k << 16)] + w[1:] if route == "header" else w, dtype=np.uint32)

    def source():
        if route == "sel":
            yield from tx["s_sel"].write(np.asarray([0, 1], dtype=np.uint32)[:1])
        x = yield from tx["s_in"].begin(frame(0, 0))                   # to output 0, held open
        yield sim.env.timeout(5 * PER)
        seen["5"] = rx[0].pending_burst is not None                      # routed and begun already
        yield sim.env.timeout(195 * PER)
        yield from tx["s_in"].end(x)
        if route == "sel":
            yield from tx["s_sel"].write(np.asarray([1], dtype=np.uint32))
        yield from tx["s_in"].write(frame(1, 1))                          # then to output 1

    def read(k):
        while True:
            words = yield from rx[k].get()
            got.append((round(sim.env.now / PER), k, int(words[0]) >> 24))

    for p in (source(), read(0), read(1)):
        sim.env.process(p)
    sim.run_sim()
    assert seen["5"]
    first, second = sorted(got)
    assert [(k, f) for _, k, f in (first, second)] == [(0, 0), (1, 1)]
    assert first[0] >= 200 and second[0] > first[0]


def test_strict_priority_drains_the_lowest_input_first():
    sel, _ = _backlogged("priority")
    assert sel[1:] == [0] * 3 + [1] * 4 + [2] * 4         # the first grant went before the backlog
    assert sorted(sel) == [0] * 4 + [1] * 4 + [2] * 4


def test_round_robin_takes_each_input_in_turn():
    sel, _ = _backlogged("round_robin")
    assert sorted(sel) == [0] * 4 + [1] * 4 + [2] * 4
    first = sel[0]
    assert sel == [(first + j) % 3 for j in range(12)]


@pytest.mark.parametrize("policy", ["priority", "round_robin"])
def test_a_frame_is_never_split_and_follows_its_index(policy):
    sel, out = _backlogged(policy, frame_bursts=2)
    assert len(out) == 2 * len(sel)
    for i, k in enumerate(sel):
        a, b = out[2 * i], out[2 * i + 1]
        assert int(a[0]) >> 8 == k and int(b[0]) >> 8 == k           # both bursts from the winner
        assert int(a[0]) & 0xF == 0 and int(b[0]) & 0xF == 1          # in order, header then payload


# --- the demux and the pair, in their harnesses -------------------------------------------------------


@pytest.mark.parametrize("route,frame_bursts", [("sel", 1), ("sel", 2), ("header", 1), ("header", 3)])
def test_the_demux_routes_every_frame_and_drops_an_unnamed_one(route, frame_bursts):
    mod = StreamDemux(name="dmx", sim=Simulation(), n_out=3, bitwidth=32, frame_bursts=frame_bursts,
                      route=route, sel_lsb=16, sel_bits=8)
    sysm = harness(mod, workload="test")
    assert sysm.dut().route == route                     # the harness's copy keeps the build field
    sysm.run()
    _assert_golden(sysm)
    assert sysm.dut().dropped == 1


@pytest.mark.parametrize("policy", ["priority", "round_robin"])
@pytest.mark.parametrize("n,frame_bursts", [(2, 1), (3, 2), (4, 1)])
def test_the_pair_returns_every_input_on_its_own_output(policy, n, frame_bursts):
    mod = ArbiterDemuxPair(name="pair", sim=Simulation(), n=n, bitwidth=32, frame_bursts=frame_bursts,
                           policy=policy)
    sysm = harness(mod, workload="test")
    assert sysm.dut().policy == policy and sysm.dut().arb.policy == policy
    sysm.run()
    _assert_golden(sysm)
    assert sum(len(v) for v in sysm.golden_outputs().values()) > 0


# --- the bodies -------------------------------------------------------------------------------------


def test_the_arbiter_body_and_ports():
    a = StreamArbiter(name="a", sim=Simulation(), n_in=3, bitwidth=64, frame_bursts=2, policy="round_robin")
    assert a.ports == ("m_out", "m_sel", "s_in_0", "s_in_1", "s_in_2")
    kt = a.kernel_task()
    assert kt.signature == a.ports and kt.framed == a.ports and kt.template_args == (64, 2, 1)


@pytest.mark.parametrize("lsb", [0, 70, 120])
def test_the_header_demux_reads_its_field_in_a_word_wider_than_64_bits(lsb):
    """A word wider than 64 bits travels as 64-bit chunks, chunk 0 the low bits: the field is read
    across them (examples/os_filterbank routes 184-bit answers by a header field)."""
    d = StreamDemux(name="d", sim=Simulation(), n_out=4, bitwidth=184, route="header", sel_lsb=lsb,
                    sel_bits=8)
    others = ((1 << 184) - 1) & ~(0xFF << lsb)          # every bit outside the field set
    for k in range(4):
        word = others | (k << lsb)
        chunks = np.asarray([(word >> (64 * i)) & ((1 << 64) - 1) for i in range(3)], dtype=np.uint64)
        assert d.index_of(chunks) == k
    assert d.index_of(np.uint64(2)) == (2 if lsb == 0 else 0)              # a plain word still works


def test_the_demux_bodies_and_ports():
    d = StreamDemux(name="d", sim=Simulation(), n_out=2, bitwidth=32)
    assert d.ports == ("s_in", "s_sel", "m_out_0", "m_out_1")
    assert d.kernel_task().task_fn == "stream_demux_task" and d.kernel_task().template_args == (32, 1)
    h = StreamDemux(name="h", sim=Simulation(), n_out=2, bitwidth=32, route="header", sel_lsb=4, sel_bits=3)
    assert h.ports == ("s_in", "m_out_0", "m_out_1")
    assert h.kernel_task().task_fn == "stream_demux_hdr_task" and h.kernel_task().template_args == (32, 1, 4, 3)


@pytest.mark.parametrize("bad", [dict(n_in=1), dict(n_in=17), dict(frame_bursts=0), dict(policy="lottery")])
def test_the_arbiter_refuses_a_bad_build(bad):
    with pytest.raises(ValueError):
        StreamArbiter(name="a", sim=Simulation(), **bad)


@pytest.mark.parametrize("bad", [dict(n_out=1), dict(route="tag"),
                                 dict(route="header", sel_lsb=30, sel_bits=4)])
def test_the_demux_refuses_a_bad_build(bad):
    with pytest.raises(ValueError):
        StreamDemux(name="d", sim=Simulation(), bitwidth=32, **bad)


@pytest.mark.parametrize("step,text", [
    (StreamArbiterStep, "template <int W, int NB, int RR, typename TO, typename TS, typename... TI>"),
    (StreamDemuxStep, "template <int W, int NB, int LSB, int BITS, typename TI, typename... TO>"),
])
def test_the_steps_copy_the_bodies(tmp_path, step, text):
    s = step(output_dir="include")
    assert text in s.generate(s.KEY, BuildConfig(root_dir=tmp_path, params={}))


def test_the_pair_top_has_both_tasks_and_both_links(tmp_path):
    mod, _top = rtl.make("pair", 3, 32, 2, "round_robin")
    rtl.generate_for(tmp_path, mod)
    (top,) = (tmp_path / "gen").glob("*.cpp")
    text = top.read_text()
    assert "stream_arbiter_task<32, 2, 1," in text and "stream_demux_task<32, 2," in text
    assert "#pragma HLS STREAM variable=data depth=16" in text
    assert "#pragma HLS STREAM variable=sel depth=4" in text


# --- at RTL -------------------------------------------------------------------------------------------


@pytest.mark.xsi
@pytest.mark.parametrize("kind,n,frame_bursts,policy,workload", [
    ("demux", 3, 2, "priority", "test"),
    ("demux_hdr", 3, 1, "priority", "test"),
    ("pair", 3, 2, "priority", "test"),
    ("pair", 3, 1, "round_robin", "test"),
    # Four inputs, input 0 running dry first: the case that hung before copy_frame was INLINE (Vitis
    # 2025.1 kept one copy_frame module for every input; plans/shared_kernel_rfsoc_timing.md).
    ("pair", 4, 1, "priority", "long"),
], ids=["demux", "demux_hdr", "pair_priority", "pair_rr", "pair4_long"])
def test_rtl_outputs_match_pysim(kind, n, frame_bursts, policy, workload):
    """THE gates: every output's recording at RTL equal to pysim's, and none empty."""
    if not (find_vivado_path() and find_vitis_path()):
        pytest.skip("XSI gate prerequisite missing: Vitis HLS + Vivado")
    root = ROOT / f"{kind[:5]}{n}{frame_bursts}{policy[0]}{workload[0]}"
    root.mkdir(parents=True, exist_ok=True)
    dag, top = rtl.build_block_dag(root / "xw", kind, n, 32, frame_bursts, policy, workload=workload)
    res = dag.run(BuildConfig(root_dir=root, params={"synth": "build"}), through="compare")
    bad = {k: r.message for k, r in res.items() if not r.success}
    assert not bad, bad
    work = root / "xw" / top
    assert sorted(p.name for p in (work / "traces").iterdir()) == [f"m_out_{k}" for k in range(n)]
    for k in range(n):
        assert (work / "traces" / f"m_out_{k}" / "words.bin").stat().st_size > 0
    assert json.loads((work / "compare.json").read_text(encoding="utf-8"))["trace_mismatches"] == []
