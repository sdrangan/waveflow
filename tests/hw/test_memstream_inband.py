"""Tests for the **in-band framed** MemRStream/MemWStream forwarding protocol (``inband=True``).

The claim under test: with ``[MemRCmd | MemWCmd | response | data]`` on ONE stream, a descriptor can
never be paired with the wrong data — the pairing is structural, not a "both streams stay in order"
invariant.  Both mem-streams relay opaquely: the reader forwards ``MemRCmd.fwd_bursts`` bursts and
appends its data; the writer buffers ``MemWCmd.fwd_bursts`` bursts across the write and echoes them.
Neither parses what it forwards, so one reader/writer serves any application.  See
``plans/memcopy_inband_integration.md``.

pysim only — the fixed C++ task bodies are verified via XSI.
"""
from __future__ import annotations

import numpy as np
import pytest

from waveflow.hw.clock import Clock
from waveflow.hw.interface import StreamIF
from waveflow.hw.memif import AXIMMCrossBarIF, assign_address_ranges
from waveflow.hw.mem_stream import MemRCmd, MemRStream, MemWCmd, MemWStream
from waveflow.hw.memory import MemComponent
from waveflow.simulation.simulation import Simulation
from waveflow.simulation.stream_tb import StreamDriver, StreamSink

MEM_DW = 64
BPW = MEM_DW // 8


def _frame(src, dst, n, response, w=MEM_DW):
    """The **bursts** a sequencer puts on the reader's s_cmd for one framed copy.

    ``[MemRCmd | MemWCmd | response]``: each logical segment is its own burst (a ``get`` consumes a
    whole burst — that is what lets each stage relay the opaque segments without parsing them).
    ``MemRCmd.fwd_bursts`` counts the bursts the reader relays (the ``MemWCmd`` + the response);
    ``MemWCmd.fwd_bursts`` counts the bursts the writer buffers and echoes (the response)."""
    resp = np.asarray(response, dtype=np.uint64)                       # one opaque response burst
    memw = np.asarray(MemWCmd(addr=dst, len=n, fwd_bursts=1).serialize(word_bw=w), dtype=np.uint64)
    relay = [memw, resp]
    memr = np.asarray(MemRCmd(addr=src, len=n, fwd_bursts=len(relay)).serialize(word_bw=w),
                      dtype=np.uint64)
    return [memr, *relay]


def _run(jobs, responses, tmp_path, arena_words=4096):
    """Build sequencer -> MemRStream(inband) -> MemWStream(inband) -> memory and run it."""
    sim = Simulation()
    clk = Clock(freq=100e6)

    mem = MemComponent(name="mem", sim=sim, inline=False, clk=clk,
                       word_size=MEM_DW, addr_size=32, nwords_tot=arena_words)
    mem.alloc(arena_words)
    expected = []
    for j, (src, dst, n) in enumerate(jobs):
        known = np.random.default_rng(1234 + j).integers(0, 1 << MEM_DW, size=n, dtype=np.uint64)
        mem._mem.write(src * BPW, known)
        expected.append(known)

    rd = MemRStream(name="rd", sim=sim, mem_dwidth=MEM_DW, inband=True, clk=clk)
    wr = MemWStream(name="wr", sim=sim, mem_dwidth=MEM_DW, inband=True, emit_done=True, clk=clk)

    # the sequencer: every segment of every job is its own burst, in order
    words = [b for (s, d, n), p in zip(jobs, responses) for b in _frame(s, d, n, p)]
    bdir = tmp_path / "cmd"
    from waveflow.utils.burst_io import write_burst_bundle
    write_burst_bundle(words, bdir)
    drv = StreamDriver(sim=sim, bitwidth=MEM_DW, in_bundle="cmd", root=tmp_path,
                       has_tlast=True)
    sink = StreamSink(sim=sim, bitwidth=MEM_DW, has_tlast=True)

    cmd_if = StreamIF(name="cmd_if", sim=sim, clk=clk, bitwidth=MEM_DW)
    cmd_if.bind(ep_name="master", endpoint=drv.stream_ep)
    cmd_if.bind(ep_name="slave", endpoint=rd.s_cmd)

    mid = StreamIF(name="mid", sim=sim, clk=clk, bitwidth=MEM_DW)   # the ONE framed stream
    mid.bind(ep_name="master", endpoint=rd.m_out)
    mid.bind(ep_name="slave", endpoint=wr.s_in)

    done_if = StreamIF(name="done_if", sim=sim, clk=clk, bitwidth=MEM_DW)
    done_if.bind(ep_name="master", endpoint=wr.s_done)
    done_if.bind(ep_name="slave", endpoint=sink.stream_ep)

    xbar = AXIMMCrossBarIF(sim=sim, clk=clk, nports_master=2, nports_slave=1, bitwidth=MEM_DW)
    xbar.bind("master_0", rd.m_mem)
    xbar.bind("master_1", wr.m_mem)
    xbar.bind("slave_0", mem.s_mm)
    assign_address_ranges([mem.s_mm], [(0, arena_words * BPW)])

    sim.run_sim()
    return mem, sink, expected


def test_inband_copy_is_bit_exact(tmp_path):
    """A framed transfer copies correctly: two descriptors, a response and the data ride one stream."""
    jobs = [(16, 512, 64)]
    mem, _sink, expected = _run(jobs, [[0xA5A5]], tmp_path)
    got = mem._mem.read(512 * BPW, 64).astype(np.uint64)
    assert np.array_equal(got, expected[0])


def test_response_is_echoed_verbatim(tmp_path):
    """The response is opaque: the reader forwards it unparsed and the writer echoes it unchanged —
    the writer constructs no completion of its own."""
    response = [0xDEADBEEF, 0x1234, 0xFFFF_FFFF_FFFF_FFFF]
    mem, sink, _ = _run([(16, 512, 32)], [response], tmp_path)
    flat = np.concatenate([np.asarray(b) for b in sink.words])
    assert [int(x) for x in flat] == response, "the writer echoes the opaque response verbatim"


def test_back_to_back_jobs_cannot_desync(tmp_path):
    """Several framed jobs in flight: each descriptor stays welded to its own data.

    This is the property the two-stream protocol only had by luck of ordering.
    """
    jobs = [(16, 600, 32), (200, 900, 64), (400, 1200, 16)]
    responses = [[j] for j in range(len(jobs))]
    mem, sink, expected = _run(jobs, responses, tmp_path)
    for (_s, dst, n), exp in zip(jobs, expected):
        got = mem._mem.read(dst * BPW, n).astype(np.uint64)
        assert np.array_equal(got, exp), f"job at dst={dst} got the wrong data"


def test_response_over_buffer_bound_fails_loudly(tmp_path):
    """max_fwd_words bounds the writer's forward BUFFER — overflowing it must raise, not truncate."""
    with pytest.raises(Exception, match="exceeds the buffer bound"):
        _run([(16, 512, 16)], [list(range(9))], tmp_path)   # default max_fwd_words = 8


def test_legacy_two_stream_path_is_untouched():
    """inband defaults to False and still exposes s_cmd — the framed mode changes no existing behavior."""
    sim = Simulation()
    w = MemWStream(name="legacy", sim=sim, mem_dwidth=MEM_DW)
    assert w.inband is False or int(w.inband) == 0
    assert hasattr(w, "s_cmd"), "the legacy protocol keeps its separate command port"
    wi = MemWStream(name="framed", sim=Simulation(), mem_dwidth=MEM_DW, inband=True)
    assert not hasattr(wi, "s_cmd"), "in-band mode has no separate command port"
