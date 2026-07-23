"""interleaver_inband.py — the interleaver rebuilt on the framework MemRStream / MemWStream.

The [module overview](../../docs/examples/interleaver/interleaver.md) design uses *custom* `il_mem_r` /
`il_mem_w` adaptors and a custom per-job token forwarded on five `cmd` edges.  This variant does the
same gather `Y[i] = X[P[i]]` but composes the **framework** in-band mem-streams instead — the same
`MemRStream` / `MemWStream(inband)` that `mem_copy` uses — so the interleaver inherits their shipped,
XSI-verified timing and the design owns only its **custom compute** (`IlCompute`, reused verbatim).

The custom token dissolves into the mem-stream's in-band descriptor forwarding:

* **`cmd_rx`** (the schema-aware framer, mem_copy's `Sequencer` role) reads one `InterleaverCmd` and
  frames the reader's command stream as **two reads**:
  `[MemRCmd(x_off, nw, fwd=1) | InterleaverCmd | MemRCmd(p_off, nw, fwd=0)]`.  The `fwd=1` on the first
  read relays the `InterleaverCmd` as a **header** ahead of the X data (the reader already forwards at
  the header — no framework change needed).
* **`MemRStream(inband)`** fires twice → `m_out = [InterleaverCmd | X | P]`.
* **`il_load`** reads the descriptor (→ `nw`), then X → `x_blk` and P → `p_blk` (a stream of blocks so
  the compute can random-access X), and forwards the descriptor + blocks to the compute.
* **`il_compute`** — the reused gather.
* **`il_store`** reads the descriptor and `y_blk` and frames the writer's stream
  `[MemWCmd(y_off, nw, fwd=1) | InterleaverCmd | Y]`.
* **`MemWStream(inband, emit_done)`** writes Y and echoes the `InterleaverCmd` on `s_done` after the
  store commits — the commit-timed completion.

`cmd_rx` and `il_store` are the only schema-aware stages (mirroring mem_copy's `Sequencer`); the
mem-streams relay opaquely, and the middle three carry the descriptor on two short `cmd` edges.

pysim-verified against the same `Y=X[P]` golden as the canonical design; codegen + XSI is the toolchain
follow-up.  Run via ``interleaver_sim.run_interleaver(comp_class=InterleaverInband)``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.hw_component import HwParam
from waveflow.hw.hw_freerun import FreeRunComp
from waveflow.hw.interface import (
    SobIFMaster,
    SobIFSlave,
    StreamIF,
    StreamIFMaster,
    StreamIFSlave,
    StreamOfBlocksIF,
)
from waveflow.hw.mem_stream import KernelTask, MemRCmd, MemRStream, MemWCmd, MemWStream
from waveflow.simulation.simobj import ProcessGen

from examples.interleaver.interleaver import (
    DEFAULT_MEM_DW,
    DEFAULT_N,
    IlCompute,
    InterleaverCmd,
    _make_word_block,
    _nwords,
)


@dataclass
class CmdRxInband(FreeRunComp):
    """Framer (mem_copy's ``Sequencer`` role): read one ``InterleaverCmd`` and frame the reader's
    command stream as two reads — X (with the descriptor relayed as a header) then P."""

    cpp_kernel_name: ClassVar[str | None] = "il_cmd_rx"
    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    n: HwParam[int] = DEFAULT_N
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        self.lw = w // 32
        self.nw = _nwords(int(self.n), self.lw)
        self.s_cmd = StreamIFSlave(name=f"{self.name}_s_cmd", sim=self.sim, bitwidth=w,
                                   has_tlast=False)
        self.cmd_out = StreamIFMaster(name=f"{self.name}_cmd_out", sim=self.sim, bitwidth=w,
                                      has_tlast=True)   # framed → MemRStream
        for ep in (self.s_cmd, self.cmd_out):
            self.add_endpoint(ep)
        self.fire_log: list[tuple[float, float]] = []

    def kernel_task(self) -> KernelTask:
        return KernelTask("il_cmd_rx_framed_task", "il_cmd_rx_framed_task.h", ("s_cmd", "cmd_out"),
                          template_args=(int(self.mem_dwidth),))

    def run_iter(self) -> ProcessGen[None]:
        w = int(self.mem_dwidth)
        nw = self.nw
        cmd = yield from self.s_cmd.get(InterleaverCmd)
        t0 = self.now
        # Read X with the descriptor relayed as a header (fwd=1); then read P (fwd=0). The reader
        # bursts nw words per read, so len is in WORDS (nw), not elements (n).
        memr_x = MemRCmd(addr=int(cmd.x_off), len=nw, fwd_bursts=1)
        memr_p = MemRCmd(addr=int(cmd.p_off), len=nw, fwd_bursts=0)
        yield from self.cmd_out.write(np.asarray(memr_x.serialize(word_bw=w), dtype=np.uint64))
        yield from self.cmd_out.write(np.asarray(cmd.serialize(word_bw=w), dtype=np.uint64))
        yield from self.cmd_out.write(np.asarray(memr_p.serialize(word_bw=w), dtype=np.uint64))
        self.fire_log.append((t0 / self.clk.period, self.now / self.clk.period))


@dataclass
class IlLoadInband(FreeRunComp):
    """Reads the framed ``[InterleaverCmd | X | P]`` off the reader, fills ``x_blk`` / ``p_blk`` (SOB),
    and forwards the descriptor to the compute."""

    cpp_kernel_name: ClassVar[str | None] = "il_load"
    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    n: HwParam[int] = DEFAULT_N
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        self.lw = w // 32
        self.nw = _nwords(int(self.n), self.lw)
        self.s_in = StreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=w,
                                  has_tlast=True)   # framed ← MemRStream
        self.cmd_out = StreamIFMaster(name=f"{self.name}_cmd_out", sim=self.sim, bitwidth=w,
                                      has_tlast=False)
        word_block = _make_word_block(w, self.nw)
        self.p_blk = SobIFMaster(name=f"{self.name}_p_blk", sim=self.sim, element_type=word_block)
        self.x_blk = SobIFMaster(name=f"{self.name}_x_blk", sim=self.sim, element_type=word_block)
        for ep in (self.s_in, self.cmd_out, self.p_blk, self.x_blk):
            self.add_endpoint(ep)
        self._dtype = np.uint32 if w <= 32 else np.uint64
        self.fire_log: list[tuple[float, float]] = []

    def kernel_task(self) -> KernelTask:
        return KernelTask("il_load_inband_task", "il_load_inband_task.h",
                          ("s_in", "cmd_out", "p_blk", "x_blk"),
                          template_args=(int(self.mem_dwidth), self.nw))

    def run_iter(self) -> ProcessGen[None]:
        nw = self.nw
        cmd = yield from self.s_in.get(InterleaverCmd)    # descriptor (header)
        t0 = self.now
        yield from self.cmd_out.write(cmd)                # forward to compute
        # X data (the reader's first firing appended it after the descriptor).
        xblock = yield from self.x_blk.acquire_write()
        xw = yield from self.s_in.get(nwords_max=nw)
        xblock[:xw.shape[0]] = xw.astype(self._dtype)
        yield from self.x_blk.commit_write(xblock)
        # P data (the reader's second firing).
        pblock = yield from self.p_blk.acquire_write()
        pw = yield from self.s_in.get(nwords_max=nw)
        pblock[:pw.shape[0]] = pw.astype(self._dtype)
        yield from self.p_blk.commit_write(pblock)
        self.fire_log.append((t0 / self.clk.period, self.now / self.clk.period))


@dataclass
class IlStoreInband(FreeRunComp):
    """Reads ``y_blk`` and frames the writer's stream ``[MemWCmd | InterleaverCmd | Y]``."""

    cpp_kernel_name: ClassVar[str | None] = "il_store"
    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    n: HwParam[int] = DEFAULT_N
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        self.lw = w // 32
        self.nw = _nwords(int(self.n), self.lw)
        self.cmd_in = StreamIFSlave(name=f"{self.name}_cmd_in", sim=self.sim, bitwidth=w,
                                    has_tlast=False)
        word_block = _make_word_block(w, self.nw)
        self.y_blk = SobIFSlave(name=f"{self.name}_y_blk", sim=self.sim, element_type=word_block)
        self.cmd_out = StreamIFMaster(name=f"{self.name}_cmd_out", sim=self.sim, bitwidth=w,
                                      has_tlast=True)   # framed → MemWStream
        for ep in (self.cmd_in, self.y_blk, self.cmd_out):
            self.add_endpoint(ep)
        self.fire_log: list[tuple[float, float]] = []

    def kernel_task(self) -> KernelTask:
        return KernelTask("il_store_inband_task", "il_store_inband_task.h",
                          ("cmd_in", "y_blk", "cmd_out"),
                          template_args=(int(self.mem_dwidth), self.nw))

    def run_iter(self) -> ProcessGen[None]:
        w = int(self.mem_dwidth)
        nw = self.nw
        cmd = yield from self.cmd_in.get(InterleaverCmd)
        t0 = self.now
        yblock = yield from self.y_blk.acquire_read()
        # Frame the writer's stream: write descriptor (addr=y_off, nw words), echo the InterleaverCmd
        # as the fwd/response (emitted on s_done after the store), then the Y data.
        memw = MemWCmd(addr=int(cmd.y_off), len=nw, fwd_bursts=1)
        yield from self.cmd_out.write(np.asarray(memw.serialize(word_bw=w), dtype=np.uint64))
        yield from self.cmd_out.write(np.asarray(cmd.serialize(word_bw=w), dtype=np.uint64))
        yield from self.cmd_out.write(np.asarray(yblock))
        yield from self.y_blk.release_read()
        self.fire_log.append((t0 / self.clk.period, self.now / self.clk.period))


@dataclass
class InterleaverInband(FreeRunComp):
    """The interleaver composed on the framework in-band mem-streams: ``cmd_rx → MemRStream →
    il_load → il_compute → il_store → MemWStream``.  Custom stages are only the framer, the two
    stream↔SOB bridges, and the reused gather; the read/write adaptors are framework."""

    cpp_kernel_name: ClassVar[str | None] = "interleaver_inband"
    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    n: HwParam[int] = DEFAULT_N
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    compute_calib_dir: "str | None" = None

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        n = int(self.n)
        self.lw = w // 32
        self.nw = _nwords(n, self.lw)

        self.rx = CmdRxInband(name=f"{self.name}_rx", sim=self.sim, mem_dwidth=w, n=n, clk=self.clk)
        self.rstream = MemRStream(name=f"{self.name}_memr", sim=self.sim, mem_dwidth=w, inband=True,
                                  clk=self.clk)
        self.load = IlLoadInband(name=f"{self.name}_load", sim=self.sim, mem_dwidth=w, n=n,
                                 clk=self.clk)
        self.compute = IlCompute(name=f"{self.name}_compute", sim=self.sim, mem_dwidth=w, n=n,
                                 clk=self.clk, calib_dir=self.compute_calib_dir)
        self.store = IlStoreInband(name=f"{self.name}_store", sim=self.sim, mem_dwidth=w, n=n,
                                   clk=self.clk)
        self.wstream = MemWStream(name=f"{self.name}_memw", sim=self.sim, mem_dwidth=w, inband=True,
                                  emit_done=True, clk=self.clk)
        for c in (self.rx, self.rstream, self.load, self.compute, self.store, self.wstream):
            self.add_comp(c)
        self.gather = self.compute          # the completion-timeline probe (job_end_cyc)

        def _sif(name, master, slave, framed=False):
            iface = StreamIF(name=f"{self.name}_{name}_if", sim=self.sim, clk=self.clk, bitwidth=w,
                             framed=framed)
            iface.bind("master", master)
            iface.bind("slave", slave)
            self.add_if(iface)

        def _sobif(name, master, slave):
            word_block = _make_word_block(w, self.nw)
            iface = StreamOfBlocksIF(name=f"{self.name}_{name}_if", sim=self.sim, clk=self.clk,
                                     element_type=word_block)
            iface.bind("master", master)
            iface.bind("slave", slave)
            self.add_if(iface)

        _sif("cmd_rd", self.rx.cmd_out, self.rstream.s_cmd, framed=True)    # [MemRCmd|desc|MemRCmd]
        _sif("rdata", self.rstream.m_out, self.load.s_in, framed=True)      # [desc|X|P]
        _sif("cmd_lc", self.load.cmd_out, self.compute.cmd_in)             # descriptor token
        _sif("cmd_cs", self.compute.cmd_out, self.store.cmd_in)            # descriptor token
        _sif("wdata", self.store.cmd_out, self.wstream.s_in, framed=True)   # [MemWCmd|resp|Y]
        _sobif("p_blk", self.load.p_blk, self.compute.p_blk)
        _sobif("x_blk", self.load.x_blk, self.compute.x_blk)
        _sobif("y_blk", self.compute.y_blk, self.store.y_blk)

        self.boundary = ["s_cmd", "m_in", "m_out", "s_done"]
        self.s_cmd = self.rx.s_cmd
        self.m_in = self.rstream.m_mem
        self.m_out = self.wstream.m_mem
        self.s_done = self.wstream.s_done
