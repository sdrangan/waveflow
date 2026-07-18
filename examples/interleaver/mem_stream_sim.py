"""mem_stream_sim.py — pysim golden harnesses for :class:`MemRStream` / :class:`MemWStream`.

The Rung-0 (pysim) leg of Gate 1: wire each component to a behavioral :class:`MemComponent` over a
``DirectMMIF`` plus a command driver and a data source/sink, run the SimPy model, and check the
functional golden — a ``MemRStream`` bursts a memory region onto a stream; a ``MemWStream`` drains a
stream into a region.  The same functional truth the generated RTL is checked against under XSI.
"""
from __future__ import annotations

import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave, Words
from waveflow.hw.memif import DirectMMIF
from waveflow.hw.memory import AddrUnit, MemComponent
from waveflow.simulation.simobj import ProcessGen, SimObj
from waveflow.simulation.simulation import Simulation
from waveflow.simulation.stream_tb import StreamDriver, StreamSink
from waveflow.utils.burst_io import write_burst_bundle

from waveflow.hw.mem_stream import (
    MRCmd,
    MWCmd,
    MemRStream,
    MemWStream,
)


# ---------------------------------------------------------------------------
# Read harness — MemRStream bursts a region onto m_out
# ---------------------------------------------------------------------------

def run_read(n_words: int = 128, base_words: int = 16, mem_dwidth: int = 64,
             byte_addressable: bool = True) -> "MemRStream":
    """Load a known word-run into memory, command a ``MemRStream`` to burst it, and check the
    stream output equals the region.  Returns the component (``transfer_spans`` carries the
    modeled overlapped read+write span for the timing check).

    ``byte_addressable`` selects the memory's addressing unit (byte-addressed AXI DRAM vs a
    word-addressed on-chip memory).  The command carries the same **word_index** either way — the
    arena convention: ``Region._word_bytes`` absorbs byte-vs-word, so both take identical commands."""
    sim = Simulation()
    clk = Clock(freq=100e6)

    addr_unit = AddrUnit.byte if byte_addressable else AddrUnit.word
    mem = MemComponent(name="mem", sim=sim, inline=False, clk=clk,
                       word_size=mem_dwidth, addr_size=32, addr_unit=addr_unit)
    # Reserve a prefix, then the burst region; both live in one flat arena at base 0 (bind_base
    # default).  The burst begins at word offset `base_words` — the command's element coordinate.
    mem.alloc(base_words)
    base_addr = mem.alloc(n_words)               # native-unit address of the burst region
    word_index = base_words                      # element coordinate within the base-0 arena
    known = (np.arange(n_words, dtype=np.uint64) * 2654435761 + 12345) & ((1 << mem_dwidth) - 1)
    mem._mem.write(base_addr, known.astype(np.uint64))

    rstream = MemRStream(name="rstream", sim=sim, mem_dwidth=mem_dwidth)
    _cmd = MRCmd(addr=word_index, len=n_words)
    with tempfile.TemporaryDirectory() as _vd:
        write_burst_bundle([np.asarray(_cmd.serialize(word_bw=mem_dwidth), dtype=np.uint64)],
                           Path(_vd) / "cmd")
        driver = StreamDriver(sim=sim, bitwidth=mem_dwidth, bundle=Path(_vd) / "cmd")
    sink = StreamSink(sim=sim, bitwidth=mem_dwidth)

    cmd_if = StreamIF(sim=sim, clk=clk, bitwidth=mem_dwidth)
    cmd_if.bind(ep_name="master", endpoint=driver.stream_ep)
    cmd_if.bind(ep_name="slave", endpoint=rstream.s_cmd)

    out_if = StreamIF(sim=sim, clk=clk, bitwidth=mem_dwidth)
    out_if.bind(ep_name="master", endpoint=rstream.m_out)
    out_if.bind(ep_name="slave", endpoint=sink.stream_ep)

    mem_if = DirectMMIF(sim=sim, clk=clk, byte_addressable=byte_addressable)
    mem_if.bind("master", rstream.m_mem)
    mem_if.bind("slave", mem.s_mm)

    sim.run_sim()

    got = np.concatenate(sink.words) if sink.words else np.array([], dtype=np.uint64)
    ok = np.array_equal(got.astype(np.uint64), known.astype(np.uint64))
    span_cyc = (rstream.transfer_spans[-1] / clk.period) if rstream.transfer_spans else 0.0
    print(f"[read] n_words={n_words} word_index={word_index} unit={addr_unit.name} got={len(got)} "
          f"words ok={ok} span={span_cyc:.1f}cyc (~n_words+fill overlap; 2*n_words is sequential)")
    assert ok, f"MemRStream mismatch:\n got={got[:8]}\n exp={known[:8]}"
    return rstream


# ---------------------------------------------------------------------------
# Write harness — MemWStream drains a stream into a region
# ---------------------------------------------------------------------------

def run_write(n_words: int = 128, base_words: int = 16, mem_dwidth: int = 64,
              byte_addressable: bool = True) -> "MemWStream":
    """Command a ``MemWStream`` to drain a known word-run off s_in into memory, then check the
    region equals the input.  Returns the component (``transfer_spans`` carries the modeled
    overlapped drain+store span).  ``byte_addressable`` selects the memory addressing unit; the
    command carries the same **word_index** either way (the arena convention)."""
    sim = Simulation()
    clk = Clock(freq=100e6)

    addr_unit = AddrUnit.byte if byte_addressable else AddrUnit.word
    mem = MemComponent(name="mem", sim=sim, inline=False, clk=clk,
                       word_size=mem_dwidth, addr_size=32, addr_unit=addr_unit)
    mem.alloc(base_words)
    base_addr = mem.alloc(n_words)               # native-unit address of the burst region
    word_index = base_words                      # element coordinate within the base-0 arena
    known = (np.arange(n_words, dtype=np.uint64) * 40503 + 7) & ((1 << mem_dwidth) - 1)

    wstream = MemWStream(name="wstream", sim=sim, mem_dwidth=mem_dwidth)
    _cmd = MWCmd(addr=word_index, len=n_words)
    with tempfile.TemporaryDirectory() as _vd:
        write_burst_bundle([np.asarray(_cmd.serialize(word_bw=mem_dwidth), dtype=np.uint64)],
                           Path(_vd) / "cmd")
        write_burst_bundle([known.astype(np.uint64)], Path(_vd) / "dat")
        cmd_drv = StreamDriver(sim=sim, bitwidth=mem_dwidth, bundle=Path(_vd) / "cmd")
        dat_drv = StreamDriver(sim=sim, bitwidth=mem_dwidth, bundle=Path(_vd) / "dat")

    cmd_if = StreamIF(sim=sim, clk=clk, bitwidth=mem_dwidth)
    cmd_if.bind(ep_name="master", endpoint=cmd_drv.stream_ep)
    cmd_if.bind(ep_name="slave", endpoint=wstream.s_cmd)

    in_if = StreamIF(sim=sim, clk=clk, bitwidth=mem_dwidth)
    in_if.bind(ep_name="master", endpoint=dat_drv.stream_ep)
    in_if.bind(ep_name="slave", endpoint=wstream.s_in)

    mem_if = DirectMMIF(sim=sim, clk=clk, byte_addressable=byte_addressable)
    mem_if.bind("master", wstream.m_mem)
    mem_if.bind("slave", mem.s_mm)

    sim.run_sim()

    got = mem._mem.read(base_addr, n_words).astype(np.uint64)
    ok = np.array_equal(got, known.astype(np.uint64))
    span_cyc = (wstream.transfer_spans[-1] / clk.period) if wstream.transfer_spans else 0.0
    print(f"[write] n_words={n_words} word_index={word_index} unit={addr_unit.name} "
          f"ok={ok} span={span_cyc:.1f}cyc")
    assert ok, f"MemWStream mismatch:\n got={got[:8]}\n exp={known[:8]}"
    return wstream


def overlap_span_cyc(comp) -> float:
    """The last modeled transfer span in cycles (``transfer_spans`` / clk period)."""
    return comp.transfer_spans[-1] / comp.clk.period


def run_and_check() -> bool:
    run_read()
    run_write()
    print("mem_stream pysim golden: PASSED")
    return True


if __name__ == "__main__":
    run_and_check()
