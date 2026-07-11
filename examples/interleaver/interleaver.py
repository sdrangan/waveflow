"""interleaver.py — Phase 4: the full generated word-granular interleaver (plans/mem_stream_impl.md).

Composes everything built so far into the complete free-running graph and generates the ap_ctrl_none
top with the SAME composite_gen.composite_top_spec — the generated analogue of the hand-written
sandbox interleaver_task_sob3.cpp (word-granular, ~295 cyc/job at n=256, nj=8, MEM_DW=64):

    s_cmd(InterleaverCmd) -> Sequencer -> MRCmd(p),MRCmd(x) -> MemRStream --mem_out--> Demux
                          -> MWCmd(y) -------------------------------------------------> MemWStream
        Demux --x_words--> Fill --SOBIF(x_blk word block)--> Gather --y_words--> MemWStream --> s_done
        Demux --p_words--------------------------------------> Gather

Six sub-components (Sequencer, MemRStream[P1.5], Demux, Fill[P3, word-granular], GatherWord, MemWStream
[P2, emit_done]) wired by six StreamEdges (mr_cmd, mw_cmd, mem_out, p_words, x_words, y_words) + one
SobEdge (x_blk) + two m_axi bundles (gmem0 read / gmem1 write) + two AXIS boundary ports.  Element-
coordinate / word_index throughout.  The word block is ap_uint<MEM_DW>[n/LW]; the Gather does LW
random elem_read<MEM_DW> reads/cycle (the dual-port ping-pong).  The job split count NW = n/LW is a
compile-time constant baked into every tile from the single generate() job-size param.

Run (project venv, from repo root)::

    PYTHONPATH=. pysilicon-venv/Scripts/python.exe examples/interleaver/interleaver.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))

from waveflow.build.build import BuildConfig, BuildDag  # noqa: E402
from waveflow.build.streamutils import MemMgrStep, MemStreamStep, StreamUtilsStep  # noqa: E402
from waveflow.hw.arrayutils import gen_array_utils  # noqa: E402
from waveflow.hw.clock import Clock  # noqa: E402
from waveflow.hw.dataschema import DataList, DataSchemaStep, IntField  # noqa: E402
from waveflow.hw.hw_component import HwComponent, HwParam  # noqa: E402
from waveflow.hw.interface import (  # noqa: E402
    SobIFMaster,
    SobIFSlave,
    StreamOfBlocksIF,
    StreamIF,
    StreamIFMaster,
    StreamIFSlave,
)
from waveflow.hw.memif import MMIFMaster  # noqa: E402
from waveflow.hw.mem_stream import (  # noqa: E402
    KernelTask,
    MRCmd,
    MWCmd,
    MemRStream,
    MemWStream,
    WORD_BW_SUPPORTED,
)
from waveflow.simulation.simobj import ProcessGen  # noqa: E402

from examples.interleaver.composite_gen import SobEdge, StreamEdge, composite_top_spec  # noqa: E402
from examples.interleaver.mem_stream_gen import GEN_DIR, INCLUDE_DIR, render_tcl, render_top  # noqa: E402
from examples.interleaver.sob_toy import Fill  # noqa: E402  (reuse the SOBIF producer, word-granular)

DEFAULT_MEM_DW = 64
DEFAULT_N = 256

# The block element type (32-bit, unsigned): its array-utils header supplies elem_read<MEM_DW> for the
# word-granular Gather.  The name fixes the header/namespace to il_elem_array_utils(.h).
IlElem = IntField.specialize(bitwidth=32, signed=False, include_dir=INCLUDE_DIR)
IlElem.__name__ = "IlElem"

# --- command field type (element/word coordinates — the word_index convention) --------------------
Word32 = IntField.specialize(bitwidth=32, signed=False)


class InterleaverCmd(DataList):
    """One app interleaver command (host -> ``s_cmd``): gather ``n`` elements ``Y[i]=X[P[i]]`` where
    P lives at word offset ``p_off``, X at ``x_off``, Y at ``y_off`` (all element/word coordinates)."""
    include_filename: ClassVar[str | None] = "il_cmd.h"
    elements = {
        "p_off": {"schema": Word32, "description": "P (index) buffer word offset"},
        "x_off": {"schema": Word32, "description": "X (source) buffer word offset"},
        "y_off": {"schema": Word32, "description": "Y (output) buffer word offset"},
        "n":     {"schema": Word32, "description": "number of elements to interleave"},
    }


#: Schema classes the gen-include step emits C++ headers for.
SCHEMA_CLASSES = [InterleaverCmd, MRCmd, MWCmd]


def _nwords(n: int, lw: int) -> int:
    """Words per array: LW 32-bit elements per MEM_DW word (ceil)."""
    return (n + lw - 1) // lw


# ---------------------------------------------------------------------------
# New pure-stream tiles (fixed bodies): Sequencer, Demux, word-granular Gather
# ---------------------------------------------------------------------------

@dataclass
class InterleaverSeq(HwComponent):
    """Decompose one :class:`InterleaverCmd` into two :class:`MRCmd` (P then X) + one :class:`MWCmd`
    (Y), all with the baked word count ``nw = n/LW``.  Pure-stream; fixed ``interleaver_seq_task``."""

    cpp_kernel_name: ClassVar[str | None] = "interleaver_seq"

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
        self.mr_cmd = StreamIFMaster(name=f"{self.name}_mr_cmd", sim=self.sim, bitwidth=w,
                                     has_tlast=False)
        self.mw_cmd = StreamIFMaster(name=f"{self.name}_mw_cmd", sim=self.sim, bitwidth=w,
                                     has_tlast=False)
        for ep in (self.s_cmd, self.mr_cmd, self.mw_cmd):
            self.add_endpoint(ep)

    def kernel_task(self) -> KernelTask:
        return KernelTask("interleaver_seq_task", "interleaver_seq_task.h",
                          ("s_cmd", "mr_cmd", "mw_cmd"),
                          template_args=(int(self.mem_dwidth), self.nw))

    def run_proc(self) -> ProcessGen[None]:
        while True:
            cmd: InterleaverCmd = yield from self.s_cmd.get(InterleaverCmd)
            yield from self.mr_cmd.write(MRCmd(word_index=int(cmd.p_off), n_words=self.nw))  # P
            yield from self.mr_cmd.write(MRCmd(word_index=int(cmd.x_off), n_words=self.nw))  # X
            yield from self.mw_cmd.write(MWCmd(word_index=int(cmd.y_off), n_words=self.nw))  # Y


@dataclass
class Demux(HwComponent):
    """Split the MemRStream ``mem_in`` run (P words then X words, ``nw`` each) into ``p_words`` (first
    ``nw``) + ``x_words`` (next ``nw``) by the baked count.  Pure-stream; fixed ``demux_task``."""

    cpp_kernel_name: ClassVar[str | None] = "demux"

    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    n: HwParam[int] = DEFAULT_N
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        self.lw = w // 32
        self.nw = _nwords(int(self.n), self.lw)
        self.mem_in = StreamIFSlave(name=f"{self.name}_mem_in", sim=self.sim, bitwidth=w,
                                    has_tlast=False)
        self.p_words = StreamIFMaster(name=f"{self.name}_p_words", sim=self.sim, bitwidth=w,
                                      has_tlast=False)
        self.x_words = StreamIFMaster(name=f"{self.name}_x_words", sim=self.sim, bitwidth=w,
                                      has_tlast=False)
        for ep in (self.mem_in, self.p_words, self.x_words):
            self.add_endpoint(ep)

    def kernel_task(self) -> KernelTask:
        return KernelTask("demux_task", "demux_task.h", ("mem_in", "p_words", "x_words"),
                          template_args=(int(self.mem_dwidth), self.nw))

    def run_proc(self) -> ProcessGen[None]:
        while True:
            pw = yield from self.mem_in.get(nwords_max=self.nw)   # P burst
            yield from self.p_words.write(np.asarray(pw))
            xw = yield from self.mem_in.get(nwords_max=self.nw)   # X burst
            yield from self.x_words.write(np.asarray(xw))


@dataclass
class GatherWord(HwComponent):
    """Word-granular Gather (the sob3 shape): read-lock the word block + ``p_in``; per output word do
    ``LW`` random ``elem_read<MEM_DW>`` block reads and pack ``LW`` results into one word.  Fixed
    ``gather_word_task``."""

    cpp_kernel_name: ClassVar[str | None] = "gather_word"

    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    n: HwParam[int] = DEFAULT_N
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        self.lw = w // 32
        self.nw = _nwords(int(self.n), self.lw)
        self.p_in = StreamIFSlave(name=f"{self.name}_p_in", sim=self.sim, bitwidth=w,
                                  has_tlast=False)
        self.x_blk = SobIFSlave(name=f"{self.name}_x_blk", sim=self.sim, bitwidth=w, block_n=self.nw)
        self.y_out = StreamIFMaster(name=f"{self.name}_y_out", sim=self.sim, bitwidth=w,
                                    has_tlast=False)
        for ep in (self.p_in, self.x_blk, self.y_out):
            self.add_endpoint(ep)
        #: gather-completion cycles, one per job — the steady-state period / throughput probe.
        self.job_end_cyc: list[float] = []

    def kernel_task(self) -> KernelTask:
        return KernelTask("gather_word_task", "gather_word_task.h", ("p_in", "x_blk", "y_out"),
                          template_args=(int(self.mem_dwidth), self.nw))

    def run_proc(self) -> ProcessGen[None]:
        lw, nw = self.lw, self.nw
        while True:
            block = yield from self.x_blk.acquire_read()          # nw packed X words (read-lock)
            pwords = yield from self.p_in.get(nwords_max=nw)       # nw packed index words
            y = np.zeros(nw, dtype=np.uint64)
            for w in range(nw):
                pword = int(pwords[w])
                yword = 0
                for lane in range(lw):
                    idx = (pword >> (32 * lane)) & 0xFFFFFFFF          # P[w*LW+lane]
                    xword = int(block[idx // lw])                     # elem_read: block word idx/LW
                    xv = (xword >> (32 * (idx % lw))) & 0xFFFFFFFF     # lane idx%LW
                    yword |= xv << (32 * lane)                        # pack LW results
                y[w] = yword
            yield from self.x_blk.release_read()                  # free the buffer (ping-pong)
            yield from self.y_out.write(y)
            self.job_end_cyc.append(self.now / self.clk.period)


# ---------------------------------------------------------------------------
# The full composite
# ---------------------------------------------------------------------------

@dataclass
class Interleaver(HwComponent):
    """The full generated interleaver composite (the sob3 graph).  Passive at this level; the six
    sub-components own the processes.  :func:`composite_top_spec` derives the generated top."""

    cpp_kernel_name: ClassVar[str | None] = "interleaver"

    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    n: HwParam[int] = DEFAULT_N
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        n = int(self.n)
        self.lw = w // 32
        self.nw = _nwords(n, self.lw)

        # --- sub-components (add_comp; insertion order == codegen task order) ---
        self.seq = InterleaverSeq(name=f"{self.name}_seq", sim=self.sim, mem_dwidth=w, n=n,
                                  clk=self.clk)
        self.rstream = MemRStream(name=f"{self.name}_r", sim=self.sim, mem_dwidth=w, clk=self.clk)
        self.demux = Demux(name=f"{self.name}_demux", sim=self.sim, mem_dwidth=w, n=n, clk=self.clk)
        self.fill = Fill(name=f"{self.name}_fill", sim=self.sim, elem_bw=w, block_n=self.nw,
                         clk=self.clk)
        self.gather = GatherWord(name=f"{self.name}_gather", sim=self.sim, mem_dwidth=w, n=n,
                                 clk=self.clk)
        self.wstream = MemWStream(name=f"{self.name}_w", sim=self.sim, mem_dwidth=w, emit_done=True,
                                  clk=self.clk)
        for c in (self.seq, self.rstream, self.demux, self.fill, self.gather, self.wstream):
            self.add_comp(c)
        self.ordered_subcomps = [self.seq, self.rstream, self.demux, self.fill, self.gather,
                                 self.wstream]

        # --- internal interfaces (add_if + bind) ---
        def _sif(name, master, slave):
            iface = StreamIF(name=f"{self.name}_{name}_if", sim=self.sim, clk=self.clk, bitwidth=w)
            iface.bind("master", master)
            iface.bind("slave", slave)
            self.add_if(iface)
            return iface

        _sif("mr_cmd", self.seq.mr_cmd, self.rstream.s_cmd)
        _sif("mw_cmd", self.seq.mw_cmd, self.wstream.s_cmd)
        _sif("mem_out", self.rstream.m_out, self.demux.mem_in)
        _sif("p_words", self.demux.p_words, self.gather.p_in)
        _sif("x_words", self.demux.x_words, self.fill.x_in)
        _sif("y_words", self.gather.y_out, self.wstream.s_in)
        self._blk_if = StreamOfBlocksIF(
            name=f"{self.name}_x_blk_if", sim=self.sim, clk=self.clk, bitwidth=w, block_n=self.nw)
        self._blk_if.bind("master", self.fill.x_blk)
        self._blk_if.bind("slave", self.gather.x_blk)
        self.add_if(self._blk_if)

        # --- graph descriptors the composite generator walks ---
        # p_words is deep: P is loaded first and buffered whole while X fills the block, so its FIFO
        # must hold >= nw words (sob3's #pragma HLS STREAM depth=1024).
        p_depth = max(1024, 2 * self.nw)
        self.internal_edges = [
            StreamEdge("mr_cmd", self.seq.mr_cmd, self.rstream.s_cmd),
            StreamEdge("mw_cmd", self.seq.mw_cmd, self.wstream.s_cmd),
            StreamEdge("mem_out", self.rstream.m_out, self.demux.mem_in),
            StreamEdge("p_words", self.demux.p_words, self.gather.p_in, depth=p_depth),
            StreamEdge("x_words", self.demux.x_words, self.fill.x_in),
            StreamEdge("y_words", self.gather.y_out, self.wstream.s_in),
            SobEdge("x_blk", self.fill.x_blk, self.gather.x_blk, elem_bw=w, block_n=self.nw),
        ]
        self.boundary = [
            ("s_cmd", self.seq.s_cmd, "axis_in", None),
            ("m_in", self.rstream.m_mem, "maxi_read", "gmem0"),
            ("m_out", self.wstream.m_mem, "maxi_write", "gmem1"),
            ("s_done", self.wstream.s_done, "axis_out", None),
        ]
        self.cmd_headers = tuple(dict.fromkeys(c.resolved_include_filename() for c in SCHEMA_CLASSES))
        self.extra_includes = ("hls_streamofblocks.h",)

        # convenience refs for the sim harness
        self.s_cmd = self.seq.s_cmd
        self.m_in = self.rstream.m_mem
        self.m_out = self.wstream.m_mem
        self.s_done = self.wstream.s_done


# ---------------------------------------------------------------------------
# P-SOB variant — the symmetric topology (both P and X are resident SOB blocks)
# ---------------------------------------------------------------------------
#
# Same Sequencer / MemRStream / MemWStream; Demux+Fill collapse into ONE SplitFill task
# (stream-in -> two SOB blocks), and Gather reads two SOBs instead of a p_words stream + one SOB.
# Simpler graph (5 tasks, no deep p_words FIFO) and a decisive A/B test of the nj=8 deadlock: only
# two SobEdges are needed (composite_gen already handles SobEdge — no generator change).


@dataclass
class SplitFill(HwComponent):
    """Merge of Demux + Fill: read the ``mem_in`` run (P words then X words, ``nw`` each) and
    write-lock-fill two resident blocks — ``p_blk`` (first ``nw``) + ``x_blk`` (next ``nw``).
    Pure-stream-in, two-SOB-out (no m_axi).  Fixed ``split_fill_task``."""

    cpp_kernel_name: ClassVar[str | None] = "split_fill"

    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    n: HwParam[int] = DEFAULT_N
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        self.lw = w // 32
        self.nw = _nwords(int(self.n), self.lw)
        self.mem_in = StreamIFSlave(name=f"{self.name}_mem_in", sim=self.sim, bitwidth=w,
                                    has_tlast=False)
        self.p_blk = SobIFMaster(name=f"{self.name}_p_blk", sim=self.sim, bitwidth=w, block_n=self.nw)
        self.x_blk = SobIFMaster(name=f"{self.name}_x_blk", sim=self.sim, bitwidth=w, block_n=self.nw)
        for ep in (self.mem_in, self.p_blk, self.x_blk):
            self.add_endpoint(ep)
        self._dtype = np.uint32 if w <= 32 else np.uint64

    def kernel_task(self) -> KernelTask:
        return KernelTask("split_fill_task", "split_fill_task.h", ("mem_in", "p_blk", "x_blk"),
                          template_args=(int(self.mem_dwidth), self.nw))

    def run_proc(self) -> ProcessGen[None]:
        nw = self.nw
        while True:
            pblock = yield from self.p_blk.acquire_write()        # P block (write-lock)
            pw = yield from self.mem_in.get(nwords_max=nw)        # P burst
            pblock[:pw.shape[0]] = pw.astype(self._dtype)
            yield from self.p_blk.commit_write(pblock)
            xblock = yield from self.x_blk.acquire_write()        # X block (write-lock)
            xw = yield from self.mem_in.get(nwords_max=nw)        # X burst
            xblock[:xw.shape[0]] = xw.astype(self._dtype)
            yield from self.x_blk.commit_write(xblock)


@dataclass
class GatherTwoSob(HwComponent):
    """Gather from two resident SOBs: read-lock ``p_blk`` (index block, read sequentially) + ``x_blk``
    (source block, random ``elem_read<MEM_DW>``); per output word do ``LW`` random reads and pack.
    Fixed ``gather_two_sob_task``."""

    cpp_kernel_name: ClassVar[str | None] = "gather_two_sob"

    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    n: HwParam[int] = DEFAULT_N
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        self.lw = w // 32
        self.nw = _nwords(int(self.n), self.lw)
        self.p_blk = SobIFSlave(name=f"{self.name}_p_blk", sim=self.sim, bitwidth=w, block_n=self.nw)
        self.x_blk = SobIFSlave(name=f"{self.name}_x_blk", sim=self.sim, bitwidth=w, block_n=self.nw)
        self.y_out = StreamIFMaster(name=f"{self.name}_y_out", sim=self.sim, bitwidth=w,
                                    has_tlast=False)
        for ep in (self.p_blk, self.x_blk, self.y_out):
            self.add_endpoint(ep)
        self.job_end_cyc: list[float] = []

    def kernel_task(self) -> KernelTask:
        return KernelTask("gather_two_sob_task", "gather_two_sob_task.h",
                          ("p_blk", "x_blk", "y_out"),
                          template_args=(int(self.mem_dwidth), self.nw))

    def run_proc(self) -> ProcessGen[None]:
        lw, nw = self.lw, self.nw
        while True:
            pblock = yield from self.p_blk.acquire_read()         # index block (read-lock)
            xblock = yield from self.x_blk.acquire_read()         # source block (read-lock)
            y = np.zeros(nw, dtype=np.uint64)
            for w in range(nw):
                pword = int(pblock[w])                            # sequential index word
                yword = 0
                for lane in range(lw):
                    idx = (pword >> (32 * lane)) & 0xFFFFFFFF          # P[w*LW+lane]
                    xword = int(xblock[idx // lw])                    # elem_read: x_blk word idx/LW
                    xv = (xword >> (32 * (idx % lw))) & 0xFFFFFFFF     # lane idx%LW
                    yword |= xv << (32 * lane)
                y[w] = yword
            yield from self.p_blk.release_read()
            yield from self.x_blk.release_read()
            yield from self.y_out.write(y)
            self.job_end_cyc.append(self.now / self.clk.period)


@dataclass
class InterleaverSob(HwComponent):
    """The P-SOB interleaver composite: ``Seq -> MemRStream -> SplitFill ->(p_blk,x_blk SOB)->
    GatherTwoSob -> MemWStream``.  Five sub-components; four StreamEdges (mr_cmd, mw_cmd, mem_out,
    y_words) + two SobEdges (p_blk, x_blk).  No deep p_words FIFO (P is now resident)."""

    cpp_kernel_name: ClassVar[str | None] = "interleaver_sob"

    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    n: HwParam[int] = DEFAULT_N
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        n = int(self.n)
        self.lw = w // 32
        self.nw = _nwords(n, self.lw)

        self.seq = InterleaverSeq(name=f"{self.name}_seq", sim=self.sim, mem_dwidth=w, n=n,
                                  clk=self.clk)
        self.rstream = MemRStream(name=f"{self.name}_r", sim=self.sim, mem_dwidth=w, clk=self.clk)
        self.split = SplitFill(name=f"{self.name}_split", sim=self.sim, mem_dwidth=w, n=n,
                               clk=self.clk)
        self.gather = GatherTwoSob(name=f"{self.name}_gather", sim=self.sim, mem_dwidth=w, n=n,
                                   clk=self.clk)
        self.wstream = MemWStream(name=f"{self.name}_w", sim=self.sim, mem_dwidth=w, emit_done=True,
                                  clk=self.clk)
        for c in (self.seq, self.rstream, self.split, self.gather, self.wstream):
            self.add_comp(c)
        self.ordered_subcomps = [self.seq, self.rstream, self.split, self.gather, self.wstream]

        def _sif(name, master, slave):
            iface = StreamIF(name=f"{self.name}_{name}_if", sim=self.sim, clk=self.clk, bitwidth=w)
            iface.bind("master", master)
            iface.bind("slave", slave)
            self.add_if(iface)

        def _sobif(name, master, slave):
            iface = StreamOfBlocksIF(name=f"{self.name}_{name}_if", sim=self.sim, clk=self.clk,
                                     bitwidth=w, block_n=self.nw)
            iface.bind("master", master)
            iface.bind("slave", slave)
            self.add_if(iface)

        _sif("mr_cmd", self.seq.mr_cmd, self.rstream.s_cmd)
        _sif("mw_cmd", self.seq.mw_cmd, self.wstream.s_cmd)
        _sif("mem_out", self.rstream.m_out, self.split.mem_in)
        _sobif("p_blk", self.split.p_blk, self.gather.p_blk)
        _sobif("x_blk", self.split.x_blk, self.gather.x_blk)
        _sif("y_words", self.gather.y_out, self.wstream.s_in)

        self.internal_edges = [
            StreamEdge("mr_cmd", self.seq.mr_cmd, self.rstream.s_cmd),
            StreamEdge("mw_cmd", self.seq.mw_cmd, self.wstream.s_cmd),
            StreamEdge("mem_out", self.rstream.m_out, self.split.mem_in),
            SobEdge("p_blk", self.split.p_blk, self.gather.p_blk, elem_bw=w, block_n=self.nw),
            SobEdge("x_blk", self.split.x_blk, self.gather.x_blk, elem_bw=w, block_n=self.nw),
            StreamEdge("y_words", self.gather.y_out, self.wstream.s_in),
        ]
        self.boundary = [
            ("s_cmd", self.seq.s_cmd, "axis_in", None),
            ("m_in", self.rstream.m_mem, "maxi_read", "gmem0"),
            ("m_out", self.wstream.m_mem, "maxi_write", "gmem1"),
            ("s_done", self.wstream.s_done, "axis_out", None),
        ]
        self.cmd_headers = tuple(dict.fromkeys(c.resolved_include_filename() for c in SCHEMA_CLASSES))
        self.extra_includes = ("hls_streamofblocks.h",)

        self.s_cmd = self.seq.s_cmd
        self.m_in = self.rstream.m_mem
        self.m_out = self.wstream.m_mem
        self.s_done = self.wstream.s_done


# ---------------------------------------------------------------------------
# Canonical six-stage variant — a forwarded per-job token through every tile
# ---------------------------------------------------------------------------
#
# The teachable load-compute-store anatomy AND the nj=8 deadlock fix: one InterleaverCmd token per job
# is emitted by cmd_rx and forwarded through every stage (five Cmd StreamEdges), so each tile is paced
# to one job in flight (sob3's structure) — the pipeline never fills to the done==#tasks+1 depth the
# mix / P-SOB variants hit.  mem_w emits the token on s_done AFTER the write burst (commit-timed done).
#
#   cmd_rx -> il_mem_r -> il_load -> il_compute -> il_store -> il_mem_w -> s_done
#            (token threaded through all six; data edges alongside)

_TOKEN = InterleaverCmd


def _word_t(mem_dwidth: int) -> type:
    return IntField.specialize(bitwidth=int(mem_dwidth), signed=False)


@dataclass
class CmdRx(HwComponent):
    """Stage 1: read the app command off the ``s_cmd`` AXIS boundary and emit the per-job token."""

    cpp_kernel_name: ClassVar[str | None] = "cmd_rx"
    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        self.s_cmd = StreamIFSlave(name=f"{self.name}_s_cmd", sim=self.sim, bitwidth=w,
                                   has_tlast=False)
        self.cmd_out = StreamIFMaster(name=f"{self.name}_cmd_out", sim=self.sim, bitwidth=w,
                                      has_tlast=False)
        for ep in (self.s_cmd, self.cmd_out):
            self.add_endpoint(ep)

    def kernel_task(self) -> KernelTask:
        return KernelTask("cmd_rx_task", "cmd_rx_task.h", ("s_cmd", "cmd_out"),
                          template_args=(int(self.mem_dwidth),))

    def run_proc(self) -> ProcessGen[None]:
        while True:
            cmd = yield from self.s_cmd.get(_TOKEN)
            yield from self.cmd_out.write(cmd)


@dataclass
class IlMemR(HwComponent):
    """Stage 2 (m_axi read owner, gmem0): token -> burst P->pwords + X->xwords -> forward token."""

    cpp_kernel_name: ClassVar[str | None] = "il_mem_r"
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
        self.m_mem = MMIFMaster(name=f"{self.name}_m_mem", sim=self.sim, bitwidth=w)
        self.cmd_out = StreamIFMaster(name=f"{self.name}_cmd_out", sim=self.sim, bitwidth=w,
                                      has_tlast=False)
        self.pwords = StreamIFMaster(name=f"{self.name}_pwords", sim=self.sim, bitwidth=w,
                                     has_tlast=False)
        self.xwords = StreamIFMaster(name=f"{self.name}_xwords", sim=self.sim, bitwidth=w,
                                     has_tlast=False)
        for ep in (self.cmd_in, self.m_mem, self.cmd_out, self.pwords, self.xwords):
            self.add_endpoint(ep)
        self._wt, self._bw = _word_t(w), w

    def kernel_task(self) -> KernelTask:
        return KernelTask("il_mem_r_task", "il_mem_r_task.h",
                          ("cmd_in", "m_mem", "cmd_out", "pwords", "xwords"),
                          template_args=(int(self.mem_dwidth), self.nw))

    def run_proc(self) -> ProcessGen[None]:
        nw = self.nw
        while True:
            cmd = yield from self.cmd_in.get(_TOKEN)
            yield from self.cmd_out.write(cmd)
            region = self.m_mem.region(0, self._wt, word_bw=self._bw)
            pw = int(cmd.p_off)
            pdata, _ = yield from region.read_slice_pipelined(pw, pw + nw)
            yield from self.pwords.write(np.asarray(pdata))
            xw = int(cmd.x_off)
            xdata, _ = yield from region.read_slice_pipelined(xw, xw + nw)
            yield from self.xwords.write(np.asarray(xdata))


@dataclass
class IlLoad(HwComponent):
    """Stage 3 (stream->SOB): token + pwords/xwords -> fill p_blk/x_blk -> forward token."""

    cpp_kernel_name: ClassVar[str | None] = "il_load"
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
        self.pwords = StreamIFSlave(name=f"{self.name}_pwords", sim=self.sim, bitwidth=w,
                                    has_tlast=False)
        self.xwords = StreamIFSlave(name=f"{self.name}_xwords", sim=self.sim, bitwidth=w,
                                    has_tlast=False)
        self.cmd_out = StreamIFMaster(name=f"{self.name}_cmd_out", sim=self.sim, bitwidth=w,
                                      has_tlast=False)
        self.p_blk = SobIFMaster(name=f"{self.name}_p_blk", sim=self.sim, bitwidth=w, block_n=self.nw)
        self.x_blk = SobIFMaster(name=f"{self.name}_x_blk", sim=self.sim, bitwidth=w, block_n=self.nw)
        for ep in (self.cmd_in, self.pwords, self.xwords, self.cmd_out, self.p_blk, self.x_blk):
            self.add_endpoint(ep)
        self._dtype = np.uint32 if w <= 32 else np.uint64

    def kernel_task(self) -> KernelTask:
        return KernelTask("il_load_task", "il_load_task.h",
                          ("cmd_in", "pwords", "xwords", "cmd_out", "p_blk", "x_blk"),
                          template_args=(int(self.mem_dwidth), self.nw))

    def run_proc(self) -> ProcessGen[None]:
        nw = self.nw
        while True:
            cmd = yield from self.cmd_in.get(_TOKEN)
            yield from self.cmd_out.write(cmd)
            pblock = yield from self.p_blk.acquire_write()
            pw = yield from self.pwords.get(nwords_max=nw)
            pblock[:pw.shape[0]] = pw.astype(self._dtype)
            yield from self.p_blk.commit_write(pblock)
            xblock = yield from self.x_blk.acquire_write()
            xw = yield from self.xwords.get(nwords_max=nw)
            xblock[:xw.shape[0]] = xw.astype(self._dtype)
            yield from self.x_blk.commit_write(xblock)


@dataclass
class IlCompute(HwComponent):
    """Stage 4 (pure SOB->SOB): token + read-lock p_blk/x_blk -> gather into y_blk -> forward token."""

    cpp_kernel_name: ClassVar[str | None] = "il_compute"
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
        self.p_blk = SobIFSlave(name=f"{self.name}_p_blk", sim=self.sim, bitwidth=w, block_n=self.nw)
        self.x_blk = SobIFSlave(name=f"{self.name}_x_blk", sim=self.sim, bitwidth=w, block_n=self.nw)
        self.cmd_out = StreamIFMaster(name=f"{self.name}_cmd_out", sim=self.sim, bitwidth=w,
                                      has_tlast=False)
        self.y_blk = SobIFMaster(name=f"{self.name}_y_blk", sim=self.sim, bitwidth=w, block_n=self.nw)
        for ep in (self.cmd_in, self.p_blk, self.x_blk, self.cmd_out, self.y_blk):
            self.add_endpoint(ep)
        self.job_end_cyc: list[float] = []

    def kernel_task(self) -> KernelTask:
        return KernelTask("il_compute_task", "il_compute_task.h",
                          ("cmd_in", "p_blk", "x_blk", "cmd_out", "y_blk"),
                          template_args=(int(self.mem_dwidth), self.nw))

    def run_proc(self) -> ProcessGen[None]:
        lw, nw = self.lw, self.nw
        while True:
            cmd = yield from self.cmd_in.get(_TOKEN)
            yield from self.cmd_out.write(cmd)
            pblock = yield from self.p_blk.acquire_read()
            xblock = yield from self.x_blk.acquire_read()
            yblock = yield from self.y_blk.acquire_write()
            for w in range(nw):
                pword = int(pblock[w])
                yword = 0
                for lane in range(lw):
                    idx = (pword >> (32 * lane)) & 0xFFFFFFFF
                    xword = int(xblock[idx // lw])
                    xv = (xword >> (32 * (idx % lw))) & 0xFFFFFFFF
                    yword |= xv << (32 * lane)
                yblock[w] = yword
            yield from self.p_blk.release_read()
            yield from self.x_blk.release_read()
            yield from self.y_blk.commit_write(yblock)
            self.job_end_cyc.append(self.now / self.clk.period)


@dataclass
class IlStore(HwComponent):
    """Stage 5 (SOB->stream): token + read-lock y_blk -> ywords stream -> forward token."""

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
        self.y_blk = SobIFSlave(name=f"{self.name}_y_blk", sim=self.sim, bitwidth=w, block_n=self.nw)
        self.cmd_out = StreamIFMaster(name=f"{self.name}_cmd_out", sim=self.sim, bitwidth=w,
                                      has_tlast=False)
        self.ywords = StreamIFMaster(name=f"{self.name}_ywords", sim=self.sim, bitwidth=w,
                                     has_tlast=False)
        for ep in (self.cmd_in, self.y_blk, self.cmd_out, self.ywords):
            self.add_endpoint(ep)

    def kernel_task(self) -> KernelTask:
        return KernelTask("il_store_task", "il_store_task.h",
                          ("cmd_in", "y_blk", "cmd_out", "ywords"),
                          template_args=(int(self.mem_dwidth), self.nw))

    def run_proc(self) -> ProcessGen[None]:
        while True:
            cmd = yield from self.cmd_in.get(_TOKEN)
            yield from self.cmd_out.write(cmd)
            yblock = yield from self.y_blk.acquire_read()
            yield from self.ywords.write(np.asarray(yblock))
            yield from self.y_blk.release_read()


@dataclass
class IlMemW(HwComponent):
    """Stage 6 (m_axi write owner, gmem1): token + ywords -> write Y -> emit token on s_done (after
    the write burst — the commit-timed completion)."""

    cpp_kernel_name: ClassVar[str | None] = "il_mem_w"
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
        self.ywords = StreamIFSlave(name=f"{self.name}_ywords", sim=self.sim, bitwidth=w,
                                    has_tlast=False)
        self.m_mem = MMIFMaster(name=f"{self.name}_m_mem", sim=self.sim, bitwidth=w)
        self.s_done = StreamIFMaster(name=f"{self.name}_s_done", sim=self.sim, bitwidth=w,
                                     has_tlast=False)
        for ep in (self.cmd_in, self.ywords, self.m_mem, self.s_done):
            self.add_endpoint(ep)
        self._wt, self._bw = _word_t(w), w

    def kernel_task(self) -> KernelTask:
        return KernelTask("il_mem_w_task", "il_mem_w_task.h",
                          ("cmd_in", "ywords", "m_mem", "s_done"),
                          template_args=(int(self.mem_dwidth), self.nw))

    def run_proc(self) -> ProcessGen[None]:
        nw = self.nw
        while True:
            cmd = yield from self.cmd_in.get(_TOKEN)
            yw = int(cmd.y_off)
            words = yield from self.ywords.get(nwords_max=nw)
            region = self.m_mem.region(0, self._wt, word_bw=self._bw)
            yield from region.write_slice_pipelined(
                yw, np.asarray(words), t_out_start=self.now, element_type=self._wt)
            yield from self.s_done.write(cmd)        # commit-timed completion token


@dataclass
class InterleaverCanon(HwComponent):
    """The canonical six-stage interleaver with a forwarded per-job token: ``cmd_rx -> il_mem_r ->
    il_load -> il_compute -> il_store -> il_mem_w``.  Five Cmd StreamEdges (the token, one per hop) +
    three data StreamEdges (pwords, xwords, ywords) + three SobEdges (p_blk, x_blk, y_blk)."""

    cpp_kernel_name: ClassVar[str | None] = "interleaver_canon"
    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    n: HwParam[int] = DEFAULT_N
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        n = int(self.n)
        self.lw = w // 32
        self.nw = _nwords(n, self.lw)

        self.rx = CmdRx(name=f"{self.name}_rx", sim=self.sim, mem_dwidth=w, clk=self.clk)
        self.memr = IlMemR(name=f"{self.name}_memr", sim=self.sim, mem_dwidth=w, n=n, clk=self.clk)
        self.load = IlLoad(name=f"{self.name}_load", sim=self.sim, mem_dwidth=w, n=n, clk=self.clk)
        self.compute = IlCompute(name=f"{self.name}_compute", sim=self.sim, mem_dwidth=w, n=n,
                                 clk=self.clk)
        self.store = IlStore(name=f"{self.name}_store", sim=self.sim, mem_dwidth=w, n=n, clk=self.clk)
        self.memw = IlMemW(name=f"{self.name}_memw", sim=self.sim, mem_dwidth=w, n=n, clk=self.clk)
        stages = [self.rx, self.memr, self.load, self.compute, self.store, self.memw]
        for c in stages:
            self.add_comp(c)
        self.ordered_subcomps = stages
        self.gather = self.compute          # the completion-timeline probe (job_end_cyc)

        def _sif(name, master, slave):
            iface = StreamIF(name=f"{self.name}_{name}_if", sim=self.sim, clk=self.clk, bitwidth=w)
            iface.bind("master", master)
            iface.bind("slave", slave)
            self.add_if(iface)

        def _sobif(name, master, slave):
            iface = StreamOfBlocksIF(name=f"{self.name}_{name}_if", sim=self.sim, clk=self.clk,
                                     bitwidth=w, block_n=self.nw)
            iface.bind("master", master)
            iface.bind("slave", slave)
            self.add_if(iface)

        # five Cmd token hops
        _sif("cmd0", self.rx.cmd_out, self.memr.cmd_in)
        _sif("cmd1", self.memr.cmd_out, self.load.cmd_in)
        _sif("cmd2", self.load.cmd_out, self.compute.cmd_in)
        _sif("cmd3", self.compute.cmd_out, self.store.cmd_in)
        _sif("cmd4", self.store.cmd_out, self.memw.cmd_in)
        # data edges
        _sif("pwords", self.memr.pwords, self.load.pwords)
        _sif("xwords", self.memr.xwords, self.load.xwords)
        _sif("ywords", self.store.ywords, self.memw.ywords)
        # block edges
        _sobif("p_blk", self.load.p_blk, self.compute.p_blk)
        _sobif("x_blk", self.load.x_blk, self.compute.x_blk)
        _sobif("y_blk", self.compute.y_blk, self.store.y_blk)

        self.internal_edges = [
            StreamEdge("cmd0", self.rx.cmd_out, self.memr.cmd_in),
            StreamEdge("cmd1", self.memr.cmd_out, self.load.cmd_in),
            StreamEdge("cmd2", self.load.cmd_out, self.compute.cmd_in),
            StreamEdge("cmd3", self.compute.cmd_out, self.store.cmd_in),
            StreamEdge("cmd4", self.store.cmd_out, self.memw.cmd_in),
            StreamEdge("pwords", self.memr.pwords, self.load.pwords),
            StreamEdge("xwords", self.memr.xwords, self.load.xwords),
            StreamEdge("ywords", self.store.ywords, self.memw.ywords),
            SobEdge("p_blk", self.load.p_blk, self.compute.p_blk, elem_bw=w, block_n=self.nw),
            SobEdge("x_blk", self.load.x_blk, self.compute.x_blk, elem_bw=w, block_n=self.nw),
            SobEdge("y_blk", self.compute.y_blk, self.store.y_blk, elem_bw=w, block_n=self.nw),
        ]
        self.boundary = [
            ("s_cmd", self.rx.s_cmd, "axis_in", None),
            ("m_in", self.memr.m_mem, "maxi_read", "gmem0"),
            ("m_out", self.memw.m_mem, "maxi_write", "gmem1"),
            ("s_done", self.memw.s_done, "axis_out", None),
        ]
        self.cmd_headers = tuple(dict.fromkeys(c.resolved_include_filename() for c in SCHEMA_CLASSES))
        self.extra_includes = ("hls_streamofblocks.h",)

        self.s_cmd = self.rx.s_cmd
        self.m_in = self.memr.m_mem
        self.m_out = self.memw.m_mem
        self.s_done = self.memw.s_done


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def gen_headers(config: BuildConfig, mem_dwidth: int = DEFAULT_MEM_DW) -> None:
    """Generate the command headers + memmgr + streamutils + the fixed task bodies + the block
    element type's array-utils header (elem_read<MEM_DW>) into ``include/``."""
    inner = BuildDag()
    inner.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    inner.add(MemMgrStep(output_dir=INCLUDE_DIR))
    inner.add(MemStreamStep(output_dir=INCLUDE_DIR))
    for cls in SCHEMA_CLASSES:
        inner.add(DataSchemaStep(cls, word_bw_supported=WORD_BW_SUPPORTED, include_dir=INCLUDE_DIR))
    results = inner.run(config, force=True)
    failed = [n for n, r in results.items() if not r.success]
    if failed:
        raise RuntimeError(f"gen-include failed: {failed}")
    # il_elem_array_utils.h — elem_read<MEM_DW> for the word-granular Gather.
    gen_array_utils(IlElem, [int(mem_dwidth)], cfg=config, streamutils_dir=INCLUDE_DIR)


def _emit_top(comp, out_dir: Path, mem_dwidth: int) -> Path:
    """Render *comp*'s composite top .cpp + csynth .tcl into ``out_dir/gen`` + ``out_dir``."""
    spec = composite_top_spec(comp, width=mem_dwidth)
    gen = out_dir / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    cpp = gen / f"{spec.top_name}.cpp"
    cpp.write_text(render_top(spec), encoding="utf-8")
    (out_dir / f"{spec.top_name}.tcl").write_text(render_tcl(spec.top_name), encoding="utf-8")
    print(f"generated {cpp.relative_to(out_dir)} + {spec.top_name}.tcl")
    return cpp


def generate(out_dir: Path = HERE, mem_dwidth: int = DEFAULT_MEM_DW, n: int = DEFAULT_N) -> Path:
    """Generate headers + the (stream/SOB-mix) Interleaver composite top .cpp + .tcl into *out_dir*."""
    from waveflow.simulation.simulation import Simulation

    config = BuildConfig(root_dir=out_dir, params={})
    gen_headers(config, mem_dwidth=mem_dwidth)
    comp = Interleaver(name="interleaver", sim=Simulation(), mem_dwidth=mem_dwidth, n=n)
    return _emit_top(comp, out_dir, mem_dwidth)


def generate_sob(out_dir: Path = HERE, mem_dwidth: int = DEFAULT_MEM_DW, n: int = DEFAULT_N) -> Path:
    """Generate headers + the P-SOB :class:`InterleaverSob` composite top .cpp + .tcl into *out_dir*."""
    from waveflow.simulation.simulation import Simulation

    config = BuildConfig(root_dir=out_dir, params={})
    gen_headers(config, mem_dwidth=mem_dwidth)
    comp = InterleaverSob(name="interleaver_sob", sim=Simulation(), mem_dwidth=mem_dwidth, n=n)
    return _emit_top(comp, out_dir, mem_dwidth)


def generate_canon(out_dir: Path = HERE, mem_dwidth: int = DEFAULT_MEM_DW, n: int = DEFAULT_N) -> Path:
    """Generate headers + the canonical six-stage :class:`InterleaverCanon` top .cpp + .tcl."""
    from waveflow.simulation.simulation import Simulation

    config = BuildConfig(root_dir=out_dir, params={})
    gen_headers(config, mem_dwidth=mem_dwidth)
    comp = InterleaverCanon(name="interleaver_canon", sim=Simulation(), mem_dwidth=mem_dwidth, n=n)
    return _emit_top(comp, out_dir, mem_dwidth)


if __name__ == "__main__":
    generate()
    generate_sob()
    generate_canon()
