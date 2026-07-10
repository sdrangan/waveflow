"""mem_stream.py — the two reusable memory-endpoint components: ``MemRStream`` / ``MemWStream``.

The Waveflow realization of the "stream-wrapped memory" pattern (``plans/component.md``): two
pre-written, reusable :class:`~waveflow.hw.hw_component.HwComponent`s whose **kernel body is FIXED**
(= the hand-validated sandbox ``a2s`` / ``s2a`` in
``examples/interleaver/sandbox/il_1d/interleaver_task_sob3.cpp``), parameterized only by ``MEM_DW``.

* :class:`MemRStream` — the sole ``m_axi`` **read** owner: dequeues an :class:`MRCmd`
  ``{byte_addr, n_words}``, converts the byte address to a word index, and bursts ``n_words``
  packed words out on ``m_out`` (word-granular, one word/cycle).
* :class:`MemWStream` — the mirror: dequeues an :class:`MWCmd`, drains ``n_words`` words off
  ``s_in`` and pure-writes them to memory (word-aligned burst).

Both are **word-granular** (``ap_uint<MEM_DW>`` throughout — the sob3 lesson: element-granular
streams halve bus bandwidth at ``MEM_DW>32``), and each is a **free-running** (``ap_ctrl_none``)
single-``hls::task`` kernel.  Per the DTLP + ``hls::task``+``m_axi`` de-risk (memories
``reference-hls-task-no-maxi`` / ``reference-hls-stream-of-blocks-pingpong``) an ``m_axi`` owner
touches **only** streams — never a ``stream_of_blocks`` — which is exactly what these two do.

**Codegen is a template, not a ``run_proc`` extraction** (``examples/interleaver/mem_stream_gen.py``):
the body is fixed, so we emit the ``hls::task`` kernel directly from a template parameterized by
``MEM_DW`` + the generated command struct.  :meth:`MemRStream.run_proc` / :meth:`MemWStream.run_proc`
here are the **pysim golden** only — the bit-exact functional twin the generated RTL is checked
against (via XSI — ``ap_ctrl_none`` cosim is unreliable).

The command struct C++ generates from the :class:`MRCmd` / :class:`MWCmd` schema (the same
``DataSchemaStep`` path that emits ``VmacCmd`` / ``FIRCmd``), so the sim ``.get()`` and the kernel
struct share one source.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList, IntField, MemAddr
from waveflow.hw.hw_component import HwComponent, HwParam
from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
from waveflow.hw.memif import MMIFMaster
from waveflow.simulation.simobj import ProcessGen

# --- command field types (fixed widths — a plain DataList, no ParamSchema) ----------------------
Addr32 = MemAddr.specialize(bitwidth=32)          # byte address into the shared region
Word32 = IntField.specialize(bitwidth=32, signed=False)   # word / element count


class MRCmd(DataList):
    """One ``MemRStream`` command (host/sequencer -> ``s_cmd``): burst ``n_words`` packed words
    starting at the word-aligned byte address ``byte_addr``."""
    elements = {
        "byte_addr": {"schema": Addr32, "description": "word-aligned byte address of the burst"},
        "n_words":   {"schema": Word32, "description": "number of packed words to read"},
    }


class MWCmd(DataList):
    """One ``MemWStream`` command (host/sequencer -> ``s_cmd``): drain ``n_words`` words off
    ``s_in`` and pure-write them starting at the word-aligned byte address ``byte_addr``."""
    elements = {
        "byte_addr": {"schema": Addr32, "description": "word-aligned byte address of the burst"},
        "n_words":   {"schema": Word32, "description": "number of packed words to write"},
    }


#: Schema classes the gen-include step emits C++ headers for (consumed by the kernel templates).
SCHEMA_CLASSES = [MRCmd, MWCmd]

#: Word widths the generated command headers (read_stream<W>) support.
WORD_BW_SUPPORTED = [32, 64]

HERE = Path(__file__).resolve().parent


def _word_bytes(mem_dwidth: int) -> int:
    """Bytes per packed memory word (byte-addressed AXI)."""
    return int(mem_dwidth) // 8


@dataclass
class MemRStream(HwComponent):
    """The sole ``m_axi`` **read** owner: an :class:`MRCmd` queue -> word-granular ``m_out`` burst.

    Endpoints (added in :meth:`__post_init__`): ``m_mem`` (:class:`MMIFMaster`, bound **read**),
    ``s_cmd`` (:class:`StreamIFSlave` carrying :class:`MRCmd`), ``m_out`` (:class:`StreamIFMaster`
    carrying packed words).  Structural param: ``mem_dwidth`` (= ``MEM_DW``).  Codegen emits the
    fixed ``a2s`` ``hls::task`` (``examples/interleaver/mem_stream_gen.py``); :meth:`run_proc` is the
    pysim golden.
    """

    cpp_kernel_name: ClassVar[str | None] = "mem_r_stream"
    cpp_namespace: ClassVar[str | None] = "mem_r_stream_impl"

    mem_dwidth: HwParam[int] = 64      # MEM_DW — memory / stream word width (LW = MEM_DW/32)
    mem_awidth: HwParam[int] = 32      # m_axi / command address width
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        # m_mem is the sole read owner — bound 'R' so a stray write is a wire-up error and the
        # generated pointer is const (the @port_read capability, plans/component.md).
        self.m_mem = MMIFMaster(
            name=f"{self.name}_m_mem", sim=self.sim, bitwidth=int(self.mem_dwidth))
        self.s_cmd = StreamIFSlave(
            name=f"{self.name}_s_cmd", sim=self.sim, bitwidth=int(self.mem_dwidth),
            has_tlast=False)
        self.m_out = StreamIFMaster(
            name=f"{self.name}_m_out", sim=self.sim, bitwidth=int(self.mem_dwidth),
            has_tlast=False)
        for ep in (self.m_mem, self.s_cmd, self.m_out):
            self.add_endpoint(ep)
        self._mem_bw = int(self.mem_dwidth)

    @property
    def Cmd(self) -> type[MRCmd]:
        return MRCmd

    def run_proc(self) -> ProcessGen[None]:
        """The pysim golden (NOT extracted — codegen is the fixed ``a2s`` template).  Free-running:
        dequeue an :class:`MRCmd`, convert its byte address to a word index, read that word-run off
        ``m_mem`` and burst it out on ``m_out``.  Blocks on an empty command queue (sim drains when
        the driver stops), mirroring the sob3 ``load_task`` firing model."""
        wbytes = _word_bytes(self._mem_bw)
        while True:
            cmd: MRCmd = yield from self.s_cmd.get(MRCmd)
            byte_addr = int(cmd.byte_addr)
            nw = int(cmd.n_words)
            assert byte_addr % wbytes == 0, (
                f"MemRStream: byte_addr {byte_addr} not word-aligned (word={wbytes}B)")
            words = yield from self.m_mem.read(nw, byte_addr)   # m_mem[w0 .. w0+nw)
            yield from self.m_out.write(words)                  # word-granular burst out


@dataclass
class MemWStream(HwComponent):
    """The mirror of :class:`MemRStream`: the sole ``m_axi`` **write** owner.  An :class:`MWCmd`
    queue + an ``s_in`` word stream -> a pure-write, word-aligned burst into ``m_mem``.

    Endpoints: ``m_mem`` (:class:`MMIFMaster`, bound **write** -> non-const pointer), ``s_cmd``
    (:class:`StreamIFSlave` carrying :class:`MWCmd`), ``s_in`` (:class:`StreamIFSlave` carrying
    packed words).  Codegen emits the fixed ``s2a`` ``hls::task``; :meth:`run_proc` is the pysim
    golden.
    """

    cpp_kernel_name: ClassVar[str | None] = "mem_w_stream"
    cpp_namespace: ClassVar[str | None] = "mem_w_stream_impl"

    mem_dwidth: HwParam[int] = 64
    mem_awidth: HwParam[int] = 32
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.m_mem = MMIFMaster(
            name=f"{self.name}_m_mem", sim=self.sim, bitwidth=int(self.mem_dwidth))
        self.s_cmd = StreamIFSlave(
            name=f"{self.name}_s_cmd", sim=self.sim, bitwidth=int(self.mem_dwidth),
            has_tlast=False)
        self.s_in = StreamIFSlave(
            name=f"{self.name}_s_in", sim=self.sim, bitwidth=int(self.mem_dwidth),
            has_tlast=False)
        for ep in (self.m_mem, self.s_cmd, self.s_in):
            self.add_endpoint(ep)
        self._mem_bw = int(self.mem_dwidth)

    @property
    def Cmd(self) -> type[MWCmd]:
        return MWCmd

    def run_proc(self) -> ProcessGen[None]:
        """The pysim golden (NOT extracted — codegen is the fixed ``s2a`` template).  Free-running:
        dequeue an :class:`MWCmd`, drain its ``n_words`` off ``s_in``, and pure-write the burst to
        ``m_mem`` at the word-aligned byte address."""
        wbytes = _word_bytes(self._mem_bw)
        while True:
            cmd: MWCmd = yield from self.s_cmd.get(MWCmd)
            byte_addr = int(cmd.byte_addr)
            nw = int(cmd.n_words)
            assert byte_addr % wbytes == 0, (
                f"MemWStream: byte_addr {byte_addr} not word-aligned (word={wbytes}B)")
            words = yield from self.s_in.get(nwords_max=nw)     # nw packed data words
            yield from self.m_mem.write(words, byte_addr)       # pure-write burst
