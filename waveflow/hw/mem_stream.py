"""mem_stream.py — the two reusable memory-endpoint components: ``MemRStream`` / ``MemWStream``.

The Waveflow realization of the "stream-wrapped memory" pattern (``plans/component.md``): two
pre-written, reusable :class:`~waveflow.hw.hw_component.HwComponent`s whose **kernel body is FIXED**
(= the hand-validated sandbox ``a2s`` / ``s2a`` in ``interleaver_task_sob3.cpp``), parameterized
only by ``MEM_DW``.  They are framework code (they depend only on ``waveflow.hw`` /
``waveflow.simulation``) so any accelerator can compose them; the ``examples/interleaver`` package
keeps only instantiation, build orchestration, and the sandbox.

* :class:`MemRStream` — the sole ``m_axi`` **read** owner: dequeues an :class:`MRCmd`
  ``{word_index, n_words}`` and bursts ``n_words`` packed words out on ``m_out`` (word-granular,
  one word/cycle).  ``word_index`` is an element/word coordinate relative to the bound buffer base
  (the addressing convention — unit-agnostic; see ``plans/component.md`` and :meth:`bind_base`).
* :class:`MemWStream` — the mirror: dequeues an :class:`MWCmd`, drains ``n_words`` words off
  ``s_in`` and pure-writes them to memory (word-aligned burst).

Both are **word-granular** (``ap_uint<MEM_DW>`` throughout — the sob3 lesson: element-granular
streams halve bus bandwidth at ``MEM_DW>32``), and each generates a **free-running**
(``ap_ctrl_none``) single-``hls::task`` kernel.  Per the DTLP + ``hls::task``+``m_axi`` de-risk
(memories ``reference-hls-task-no-maxi`` / ``reference-hls-stream-of-blocks-pingpong``) an ``m_axi``
owner touches **only** streams — never a ``stream_of_blocks`` — which is exactly what these two do.

**Codegen is a template, not a ``run_proc`` extraction** (``waveflow.build.mem_stream_gen``): the
body is fixed, so the generated top bakes a concrete width and instantiates the
``template<int MEM_DW>`` task body shipped in ``waveflow/build/mem_{r,w}_stream_task.h``.
:meth:`MemRStream.run_proc` / :meth:`MemWStream.run_proc` here are the **pysim golden** only — the
bit-exact functional twin the generated RTL is checked against (via XSI — ``ap_ctrl_none`` cosim is
unreliable).

**Timing = overlapped a2s/s2a (loosely-timed).**  The hardware ``a2s`` reads word ``w`` and writes
it out the same cycle (II=1), so read and write **overlap** — the burst costs ``~n_words + fill``,
not the ``~2·n_words`` of a sequential read-then-write.  The golden models this by reading through a
word-typed :class:`~waveflow.hw.memif.Region` (which owns the byte↔word conversion) with the
**pipelined** slice and **early-anchoring** the output write at the read's first-word-available time
plus a small pipeline ``fill`` — :meth:`~waveflow.hw.interface.StreamIFMaster.write_pipelined`'s
"the wait shortens if the anchor is already past" is what folds the two spans together.  (Known
simplification, deferred: the downstream consumer still treats the whole burst as arriving at
``tend``; the named exit is a ``SimField`` — see ``plans/component.md``.)

The command struct C++ generates from the :class:`MRCmd` / :class:`MWCmd` schema (the same
``DataSchemaStep`` path that emits ``VmacCmd`` / ``FIRCmd``), so the sim ``.get()`` and the kernel
struct share one source.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataArray, IntField, ParamSchema
from waveflow.hw.hw_component import HwParam
from waveflow.hw.hw_freerun import FreeRunComp
from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
from waveflow.hw.memif import MMIFMaster
from waveflow.hw.param import Param
from waveflow.simulation.simobj import ProcessGen

# --- command field types --------------------------------------------------------------------------
# ``addr`` is an **element / word coordinate** relative to the bound buffer base, NOT a byte
# address — the addressing convention (plans/component.md): ``m_mem`` is already a word pointer, so a
# word-index command needs no byte<->word conversion in generated logic; the physical base lives in
# the ``offset=slave`` register (set once, via :meth:`bind_base`) and ``Region._word_bytes`` + the AXI
# hardware absorb byte-vs-word.  So the command is unit-agnostic.
Word32 = IntField.specialize(bitwidth=32, signed=False)   # word / element coordinate or count

# ``MRCmd``/``MWCmd``/``MemComplete`` are ``ParamSchema``s parametrized only by ``max_xfer_len`` (the
# capacity of the opaque ``xfer_msg`` correlation cookie). ``cpp_repr``/``include_filename`` are fixed
# ClassVars — NOT overridden by ``ParamSchema.specialize()``'s subclass-creation (which only replaces
# ``elements``) — so every specialization still emits the same stable C++ struct name / header
# filename: the hand-written task headers (``#include "m_r_cmd.h"``, ``struct MRCmd``) and
# ``mem_stream_gen.py``'s use of the bare classes for header generation stay valid unchanged.


class MRCmd(ParamSchema):
    """One ``MemRStream`` command (host/sequencer -> ``s_cmd``): burst ``len`` packed words
    starting at ``addr`` (element/word offset within the bound buffer, unit-agnostic).  ``xfer_msg``
    is an opaque per-job correlation cookie (a fixed-capacity :class:`~waveflow.hw.dataschema.DataArray`
    of ``max_xfer_len`` words; ``xfer_len`` carries the active count) round-tripped unmodified on the
    ``s_done`` completion echo (:class:`MemComplete`)."""
    cpp_repr: ClassVar[str] = "MRCmd"
    include_filename: ClassVar[str] = "m_r_cmd.h"

    max_xfer_len = Param(8)
    elements = {
        "addr":      {"schema": Word32, "description": "element/word offset within the bound buffer"},
        "len":       {"schema": Word32, "description": "number of packed words to read"},
        "xfer_len":  {"schema": Word32, "description": "active length of xfer_msg (<= max_xfer_len)"},
        "xfer_msg":  {
            "schema": DataArray.specialize(element_type=Word32, max_shape=(max_xfer_len,)),
            "description": "opaque per-job correlation cookie, round-tripped on completion",
        },
    }


class MWCmd(ParamSchema):
    """One ``MemWStream`` command (host/sequencer -> ``s_cmd``): drain ``len`` words off ``s_in``
    and pure-write them starting at ``addr`` (element/word offset, unit-agnostic).  ``xfer_msg``
    mirrors :class:`MRCmd`'s correlation cookie."""
    cpp_repr: ClassVar[str] = "MWCmd"
    include_filename: ClassVar[str] = "m_w_cmd.h"

    max_xfer_len = Param(8)
    elements = {
        "addr":      {"schema": Word32, "description": "element/word offset within the bound buffer"},
        "len":       {"schema": Word32, "description": "number of packed words to write"},
        "xfer_len":  {"schema": Word32, "description": "active length of xfer_msg (<= max_xfer_len)"},
        "xfer_msg":  {
            "schema": DataArray.specialize(element_type=Word32, max_shape=(max_xfer_len,)),
            "description": "opaque per-job correlation cookie, round-tripped on completion",
        },
    }


class MemComplete(ParamSchema):
    """The completion echo a ``MemWStream``/``MemRStream`` with ``emit_done=True`` writes on
    ``s_done``: the words transferred (``len``) plus the command's ``xfer_msg`` cookie, echoed back
    unmodified — so the caller can correlate the completion with the job it issued."""
    cpp_repr: ClassVar[str] = "MemComplete"
    include_filename: ClassVar[str] = "mem_complete.h"

    max_xfer_len = Param(8)
    elements = {
        "len":       {"schema": Word32, "description": "number of words transferred"},
        "xfer_len":  {"schema": Word32, "description": "valid length of the echoed xfer_msg payload"},
        "xfer_msg":  {
            "schema": DataArray.specialize(element_type=Word32, max_shape=(max_xfer_len,)),
            "description": "the command's xfer_msg, echoed back unmodified",
        },
    }


#: The (shared, cached) ``xfer_msg`` array schema at the default ``max_xfer_len=8`` — needed as its
#: own :class:`~waveflow.hw.dataschema.DataSchemaStep` entry (a ``DataArray`` gen-includes its own
#: header; ``MRCmd``/``MWCmd``/``MemComplete``'s headers only ``#include`` it, they don't emit it).
XferMsgArr = MRCmd.elements["xfer_msg"]["schema"]


@dataclass(frozen=True)
class KernelTask:
    """The fixed ``hls::task`` body descriptor a composable component exposes for the composite
    codegen (:func:`~examples.interleaver.mem_copy.composite_top_spec`).

    * ``task_fn`` — the width-templated body function (e.g. ``mem_r_stream_task``).
    * ``header`` — the copied body header to ``#include`` (e.g. ``mem_r_stream_task.h``).
    * ``signature`` — the component's **endpoint attribute names in task-argument order**.  The
      composite generator resolves each attr's endpoint to either a top-level port name or an
      internal FIFO/block name (from the interface graph), yielding the concrete call args.  This is
      the seam that makes the top *graph-derived* rather than hand-written.
    * ``template_args`` — the baked-concrete C++ template arguments in order (``(mem_dwidth,)`` for a
      width-templated body; ``(elem_bw, N)`` for the ``<EW, N>`` compute tiles)."""
    task_fn: str
    header: str
    signature: tuple[str, ...]
    template_args: tuple[int, ...] = ()


#: Schema classes the gen-include step emits C++ headers for (consumed by the kernel templates).
SCHEMA_CLASSES = [MRCmd, MWCmd, MemComplete, XferMsgArr]

#: Word widths the generated command headers (read_stream<W>) support.
WORD_BW_SUPPORTED = [32, 64]

#: Pipeline-fill latency (cycles) between the a2s/s2a read and its overlapped write — the small
#: ramp before the first word is forwarded.  Loosely-timed; the burst then costs ~n_words + fill.
FILL_CYCLES = 8


def _word_type(mem_dwidth: int) -> type[IntField]:
    """The packed-memory word element type — one ``ap_uint<MEM_DW>`` word.  Used as the Region
    element type so an element coordinate **is** a word index (``nwords_per_inst == 1``)."""
    return IntField.specialize(bitwidth=int(mem_dwidth), signed=False)


@dataclass
class MemRStream(FreeRunComp):
    """The sole ``m_axi`` **read** owner: an :class:`MRCmd` queue -> word-granular ``m_out`` burst.

    Endpoints (added in :meth:`__post_init__`): ``m_mem`` (:class:`MMIFMaster`, bound **read**),
    ``s_cmd`` (:class:`StreamIFSlave` carrying :class:`MRCmd`), ``m_out`` (:class:`StreamIFMaster`
    carrying packed words).  Structural param: ``mem_dwidth`` (= ``MEM_DW``).  Codegen emits the
    fixed ``a2s`` ``hls::task``; :meth:`run_proc` is the pysim golden.
    """

    cpp_kernel_name: ClassVar[str | None] = "mem_r_stream"
    cpp_namespace: ClassVar[str | None] = "mem_r_stream_impl"

    mem_dwidth: HwParam[int] = 64      # MEM_DW — memory / stream word width (LW = MEM_DW/32)
    mem_awidth: HwParam[int] = 32      # m_axi / command address width
    max_xfer_len: HwParam[int] = 8     # xfer_msg capacity — bridged into MRCmd/MemComplete's Param
    #: When ``True`` the endpoint also exposes an ``s_done`` :class:`StreamIFMaster` and emits a
    #: :class:`MemComplete` echo (words read + the command's ``xfer_msg`` cookie) per job.  Default
    #: ``False`` keeps the standalone Gate-1 kernel (3-arg ``mem_r_stream_task``) unchanged.
    emit_done: bool = False
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self._cmd_cls = MRCmd.specialize(max_xfer_len=int(self.max_xfer_len))
        self._complete_cls = MemComplete.specialize(max_xfer_len=int(self.max_xfer_len))
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
        eps = [self.m_mem, self.s_cmd, self.m_out]
        if self.emit_done:
            self.s_done = StreamIFMaster(
                name=f"{self.name}_s_done", sim=self.sim, bitwidth=int(self.mem_dwidth),
                has_tlast=False)
            eps.append(self.s_done)
        for ep in eps:
            self.add_endpoint(ep)
        self._mem_bw = int(self.mem_dwidth)
        self._word_t = _word_type(self.mem_dwidth)
        self._fill = FILL_CYCLES * self.clk.period
        #: Physical base of the bound buffer (the ``offset=slave`` register value, host domain) —
        #: set once via :meth:`bind_base`.  Default 0: the single-arena / flat-array mode (sim & BFM),
        #: where the command frame is base-relative so "base 0" holds in the command coordinate system.
        self._base = 0
        #: Per-command modeled transfer span (seconds) — read-start → write-complete.  Overlapped,
        #: so ~ (n_words + fill)·period, not ~2·n_words·period (observability / the timing test).
        self.transfer_spans: list[float] = []

    def bind_base(self, base: int = 0) -> None:
        """Set the bound buffer's physical base (the ``offset=slave`` register, host domain).

        The addressing convention (plans/component.md): the host writes the buffer's physical base
        **once**, then issues commands in **word offsets** within it.  The base is a native-unit
        address (``Region._word_bytes`` + the AXI hardware absorb byte-vs-word); the default 0 is the
        flat single-arena mode used by the sim harness and the BFM."""
        self._base = int(base)

    @property
    def Cmd(self) -> type[MRCmd]:
        return self._cmd_cls

    def kernel_task(self) -> "KernelTask":
        """The fixed ``hls::task`` body descriptor for the composite codegen.  The ``emit_done``
        variant is the 4-arg ``mem_r_stream_done_task`` (adds ``s_done``); the default is the
        standalone 3-arg ``mem_r_stream_task`` (unchanged Gate-1 body)."""
        if self.emit_done:
            return KernelTask(
                "mem_r_stream_done_task", "mem_r_stream_done_task.h",
                ("s_cmd", "m_mem", "m_out", "s_done"), template_args=(int(self.mem_dwidth),))
        return KernelTask("mem_r_stream_task", "mem_r_stream_task.h", ("s_cmd", "m_mem", "m_out"),
                          template_args=(int(self.mem_dwidth),))

    def run_iter(self) -> ProcessGen[None]:
        """The pysim golden — one firing = one command (NOT extracted; codegen is the fixed ``a2s``
        template).  Dequeue an :class:`MRCmd`, read the word-run off ``m_mem`` through a word-typed
        :class:`~waveflow.hw.memif.Region` (which owns byte↔word), and burst it out on ``m_out``
        **early-anchored** so the read and write overlap (``~n_words + fill``).  With ``emit_done`` it
        then writes a :class:`MemComplete` echo (words read + the command's ``xfer_msg`` cookie) on
        ``s_done``."""
        cmd = yield from self.s_cmd.get(self._cmd_cls)
        w0 = int(cmd.addr)
        nw = int(cmd.len)
        t_start = self.now
        # The command is an element coordinate relative to the buffer base: index the word-typed
        # Region (base = the bound physical base) by [addr, addr+len).  Region owns byte↔word
        # (byte_of()/word_bw), so no hand-rolled byte_addr_to_word_index / align.
        region = self.m_mem.region(self._base, self._word_t, word_bw=self._mem_bw)
        words, t0 = yield from region.read_slice_pipelined(w0, w0 + nw)
        # early-anchor the output at the first-word-available time + pipeline fill: the read and
        # write OVERLAP (write_pipelined shortens its wait when the anchor is already past).
        yield from self.m_out.write_pipelined(words, t_out_start=t0 + self._fill)
        self.transfer_spans.append(self.now - t_start)
        if self.emit_done:
            complete = self._complete_cls(len=nw, xfer_len=int(cmd.xfer_len), xfer_msg=cmd.xfer_msg)
            yield from self.s_done.write(complete)


@dataclass
class MemWStream(FreeRunComp):
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
    max_xfer_len: HwParam[int] = 8     # xfer_msg capacity — bridged into MWCmd/MemComplete's Param
    #: When ``True`` the endpoint also exposes an ``s_done`` :class:`StreamIFMaster` and emits one
    #: :class:`MemComplete` echo (words written + the command's ``xfer_msg`` cookie) per job — the
    #: composition variant used by the ``MemCopy`` composite (its fixed body is
    #: ``mem_w_stream_done_task``).  Default ``False`` keeps the standalone Gate-1 kernel (3-arg
    #: ``mem_w_stream_task``) unchanged.
    emit_done: bool = False
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self._cmd_cls = MWCmd.specialize(max_xfer_len=int(self.max_xfer_len))
        self._complete_cls = MemComplete.specialize(max_xfer_len=int(self.max_xfer_len))
        self.m_mem = MMIFMaster(
            name=f"{self.name}_m_mem", sim=self.sim, bitwidth=int(self.mem_dwidth))
        self.s_cmd = StreamIFSlave(
            name=f"{self.name}_s_cmd", sim=self.sim, bitwidth=int(self.mem_dwidth),
            has_tlast=False)
        self.s_in = StreamIFSlave(
            name=f"{self.name}_s_in", sim=self.sim, bitwidth=int(self.mem_dwidth),
            has_tlast=False)
        eps = [self.m_mem, self.s_cmd, self.s_in]
        if self.emit_done:
            self.s_done = StreamIFMaster(
                name=f"{self.name}_s_done", sim=self.sim, bitwidth=int(self.mem_dwidth),
                has_tlast=False)
            eps.append(self.s_done)
        for ep in eps:
            self.add_endpoint(ep)
        self._mem_bw = int(self.mem_dwidth)
        self._word_t = _word_type(self.mem_dwidth)
        self._fill = FILL_CYCLES * self.clk.period
        #: Physical base of the bound buffer (the ``offset=slave`` register value) — see
        #: :meth:`MemRStream.bind_base`.  Default 0: the flat single-arena mode (sim & BFM).
        self._base = 0
        self.transfer_spans: list[float] = []

    def bind_base(self, base: int = 0) -> None:
        """Set the bound buffer's physical base (the ``offset=slave`` register, host domain) — the
        mirror of :meth:`MemRStream.bind_base`.  Commands then carry word offsets within it."""
        self._base = int(base)

    @property
    def Cmd(self) -> type[MWCmd]:
        return self._cmd_cls

    def kernel_task(self) -> KernelTask:
        """The fixed ``hls::task`` body descriptor for the composite codegen.  The ``emit_done``
        variant is the 4-arg ``mem_w_stream_done_task`` (adds ``s_done``); the default is the
        standalone 3-arg ``mem_w_stream_task`` (unchanged Gate-1 body)."""
        if self.emit_done:
            return KernelTask(
                "mem_w_stream_done_task", "mem_w_stream_done_task.h",
                ("s_cmd", "s_in", "m_mem", "s_done"), template_args=(int(self.mem_dwidth),))
        return KernelTask("mem_w_stream_task", "mem_w_stream_task.h", ("s_cmd", "s_in", "m_mem"),
                          template_args=(int(self.mem_dwidth),))

    def run_iter(self) -> ProcessGen[None]:
        """The pysim golden — one firing = one command (NOT extracted; codegen is the fixed ``s2a``
        template).  Dequeue an :class:`MWCmd`, drain its ``len`` words off ``s_in`` (pipelined — the
        first-word anchor), and pure-write the burst through a word-typed
        :class:`~waveflow.hw.memif.Region` **early-anchored** so the drain and store overlap.  With
        ``emit_done`` it then writes a :class:`MemComplete` echo (words written + the command's
        ``xfer_msg`` cookie) on ``s_done``."""
        cmd = yield from self.s_cmd.get(self._cmd_cls)
        w0 = int(cmd.addr)
        nw = int(cmd.len)
        t_start = self.now
        words, t0 = yield from self.s_in.get_pipelined(self._word_t, count=nw)
        # Element coordinate relative to the buffer base: index the word-typed Region (base = the
        # bound physical base) at [addr, ...).  Region owns byte↔word (no hand conversion).
        region = self.m_mem.region(self._base, self._word_t, word_bw=self._mem_bw)
        yield from region.write_slice_pipelined(
            w0, words, t_out_start=t0 + self._fill, element_type=self._word_t)
        self.transfer_spans.append(self.now - t_start)
        if self.emit_done:
            complete = self._complete_cls(len=nw, xfer_len=int(cmd.xfer_len), xfer_msg=cmd.xfer_msg)
            yield from self.s_done.write(complete)
