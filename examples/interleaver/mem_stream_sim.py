"""mem_stream_sim.py — pysim golden harnesses for :class:`MemRStream` / :class:`MemWStream`.

The Rung-0 (pysim) leg of Gate 1: wire each component to a behavioral :class:`MemComponent` over a
``DirectMMIF`` plus a command driver and a data source/sink, run the SimPy model, and check the
functional golden — a ``MemRStream`` bursts a memory region onto a stream; a ``MemWStream`` drains a
stream into a region.  The same functional truth the generated RTL is checked against under XSI.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave, Words
from waveflow.hw.memif import DirectMMIF
from waveflow.hw.memory import MemComponent
from waveflow.simulation.simobj import ProcessGen, SimObj
from waveflow.simulation.simulation import Simulation

from waveflow.hw.mem_stream import (
    MRCmd,
    MWCmd,
    MemRStream,
    MemWStream,
)


# ---------------------------------------------------------------------------
# Tiny drivers / sinks
# ---------------------------------------------------------------------------

@dataclass
class CmdDriver(SimObj):
    """Sends a list of command-schema instances onto a stream (one burst each)."""

    cmds: list = field(default_factory=list)
    bitwidth: int = 64

    def __post_init__(self) -> None:
        super().__post_init__()
        self.stream_ep = StreamIFMaster(sim=self.sim, bitwidth=self.bitwidth, has_tlast=False)

    def run_proc(self) -> ProcessGen[None]:
        for c in self.cmds:
            yield from self.stream_ep.write(c)


@dataclass
class WordDriver(SimObj):
    """Sends raw word bursts onto a stream (the ``MemWStream`` data source)."""

    bursts: list = field(default_factory=list)   # list of np.uint word arrays
    bitwidth: int = 64

    def __post_init__(self) -> None:
        super().__post_init__()
        self.stream_ep = StreamIFMaster(sim=self.sim, bitwidth=self.bitwidth, has_tlast=False)

    def run_proc(self) -> ProcessGen[None]:
        for b in self.bursts:
            yield from self.stream_ep.write(np.asarray(b))


@dataclass
class WordSink(SimObj):
    """Collects raw word bursts off a stream (the ``MemRStream`` output sink)."""

    bitwidth: int = 64

    def __post_init__(self) -> None:
        super().__post_init__()
        self.words: list[np.ndarray] = []
        self.stream_ep = StreamIFSlave(
            sim=self.sim, bitwidth=self.bitwidth, has_tlast=False,
            rx_proc=self.rx_proc, queue_size=64)

    def rx_proc(self, words: Words) -> ProcessGen[None]:
        self.words.append(np.array(words, copy=True))
        yield self.timeout(0)


# ---------------------------------------------------------------------------
# Read harness — MemRStream bursts a region onto m_out
# ---------------------------------------------------------------------------

def run_read(n_words: int = 128, base_words: int = 16, mem_dwidth: int = 64) -> "MemRStream":
    """Load a known word-run into memory, command a ``MemRStream`` to burst it, and check the
    stream output equals the region.  Returns the component (``transfer_spans`` carries the
    modeled overlapped read+write span for the timing check)."""
    sim = Simulation()
    clk = Clock(freq=100e6)

    mem = MemComponent(name="mem", sim=sim, inline=False, clk=clk,
                       word_size=mem_dwidth, addr_size=32)
    # Reserve up to the region and pre-load a known pattern (raw words, untimed).
    mem.alloc(base_words)
    base_addr = mem.alloc(n_words)               # byte address of the burst region
    known = (np.arange(n_words, dtype=np.uint64) * 2654435761 + 12345) & ((1 << mem_dwidth) - 1)
    mem._mem.write(base_addr, known.astype(np.uint64))

    rstream = MemRStream(name="rstream", sim=sim, mem_dwidth=mem_dwidth)
    driver = CmdDriver(sim=sim, bitwidth=mem_dwidth,
                       cmds=[MRCmd(byte_addr=base_addr, n_words=n_words)])
    sink = WordSink(sim=sim, bitwidth=mem_dwidth)

    cmd_if = StreamIF(sim=sim, clk=clk, bitwidth=mem_dwidth)
    cmd_if.bind(ep_name="master", endpoint=driver.stream_ep)
    cmd_if.bind(ep_name="slave", endpoint=rstream.s_cmd)

    out_if = StreamIF(sim=sim, clk=clk, bitwidth=mem_dwidth)
    out_if.bind(ep_name="master", endpoint=rstream.m_out)
    out_if.bind(ep_name="slave", endpoint=sink.stream_ep)

    mem_if = DirectMMIF(sim=sim, clk=clk)
    mem_if.bind("master", rstream.m_mem)
    mem_if.bind("slave", mem.s_mm)

    sim.run_sim()

    got = np.concatenate(sink.words) if sink.words else np.array([], dtype=np.uint64)
    ok = np.array_equal(got.astype(np.uint64), known.astype(np.uint64))
    span_cyc = (rstream.transfer_spans[-1] / clk.period) if rstream.transfer_spans else 0.0
    print(f"[read] n_words={n_words} base=0x{base_addr:x} got={len(got)} words "
          f"ok={ok} span={span_cyc:.1f}cyc (~n_words+fill overlap; 2*n_words is sequential)")
    assert ok, f"MemRStream mismatch:\n got={got[:8]}\n exp={known[:8]}"
    return rstream


# ---------------------------------------------------------------------------
# Write harness — MemWStream drains a stream into a region
# ---------------------------------------------------------------------------

def run_write(n_words: int = 128, base_words: int = 16, mem_dwidth: int = 64) -> "MemWStream":
    """Command a ``MemWStream`` to drain a known word-run off s_in into memory, then check the
    region equals the input.  Returns the component (``transfer_spans`` carries the modeled
    overlapped drain+store span)."""
    sim = Simulation()
    clk = Clock(freq=100e6)

    mem = MemComponent(name="mem", sim=sim, inline=False, clk=clk,
                       word_size=mem_dwidth, addr_size=32)
    mem.alloc(base_words)
    base_addr = mem.alloc(n_words)
    known = (np.arange(n_words, dtype=np.uint64) * 40503 + 7) & ((1 << mem_dwidth) - 1)

    wstream = MemWStream(name="wstream", sim=sim, mem_dwidth=mem_dwidth)
    cmd_drv = CmdDriver(sim=sim, bitwidth=mem_dwidth,
                        cmds=[MWCmd(byte_addr=base_addr, n_words=n_words)])
    dat_drv = WordDriver(sim=sim, bitwidth=mem_dwidth, bursts=[known.astype(np.uint64)])

    cmd_if = StreamIF(sim=sim, clk=clk, bitwidth=mem_dwidth)
    cmd_if.bind(ep_name="master", endpoint=cmd_drv.stream_ep)
    cmd_if.bind(ep_name="slave", endpoint=wstream.s_cmd)

    in_if = StreamIF(sim=sim, clk=clk, bitwidth=mem_dwidth)
    in_if.bind(ep_name="master", endpoint=dat_drv.stream_ep)
    in_if.bind(ep_name="slave", endpoint=wstream.s_in)

    mem_if = DirectMMIF(sim=sim, clk=clk)
    mem_if.bind("master", wstream.m_mem)
    mem_if.bind("slave", mem.s_mm)

    sim.run_sim()

    got = mem._mem.read(base_addr, n_words).astype(np.uint64)
    ok = np.array_equal(got, known.astype(np.uint64))
    span_cyc = (wstream.transfer_spans[-1] / clk.period) if wstream.transfer_spans else 0.0
    print(f"[write] n_words={n_words} base=0x{base_addr:x} ok={ok} span={span_cyc:.1f}cyc")
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
