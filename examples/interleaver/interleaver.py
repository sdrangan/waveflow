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
    SobIFSlave,
    StreamOfBlocksIF,
    StreamIF,
    StreamIFMaster,
    StreamIFSlave,
)
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


def generate(out_dir: Path = HERE, mem_dwidth: int = DEFAULT_MEM_DW, n: int = DEFAULT_N) -> Path:
    """Generate headers + the Interleaver composite top .cpp + its csynth .tcl into *out_dir*."""
    from waveflow.simulation.simulation import Simulation

    config = BuildConfig(root_dir=out_dir, params={})
    gen_headers(config, mem_dwidth=mem_dwidth)

    comp = Interleaver(name="interleaver", sim=Simulation(), mem_dwidth=mem_dwidth, n=n)
    spec = composite_top_spec(comp, width=mem_dwidth)

    gen = out_dir / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    cpp = gen / f"{spec.top_name}.cpp"
    cpp.write_text(render_top(spec), encoding="utf-8")
    tcl = out_dir / f"{spec.top_name}.tcl"
    tcl.write_text(render_tcl(spec.top_name), encoding="utf-8")
    print(f"generated {cpp.relative_to(out_dir)} + {tcl.name}")
    return cpp


if __name__ == "__main__":
    generate()
