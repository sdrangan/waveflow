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

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataArray, DataList, IntField, ParamSchema
from waveflow.hw.hw_component import HwParam
from waveflow.hw.hw_freerun import FreeRunComp
from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
from waveflow.hw.memif import MMIFMaster, MMIFReadMaster, MMIFWriteMaster
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


# ---------------------------------------------------------------------------------------------
# In-band framed descriptors (``inband=True``) — see plans/memstream_inband.md
#
# The framed alternative to the two-stream (s_cmd + s_in) protocol.  One stream carries
# ``[descriptor | payload (xfer_len words) | data (data_len words)]``, so the descriptor and its data
# are contiguous and CANNOT be mispaired — the pairing is structural rather than an unenforced
# "both streams stay in order" invariant.
#
# Note what is NOT here: no fixed ``xfer_msg`` array.  The payload rides in-band with a length prefix,
# so the wire cost is per-instance (``xfer_len`` words) and the only bound is the consumer's local
# buffer.  That is what removes the 32-bit/8-word cookie and its reserialization.
# ---------------------------------------------------------------------------------------------


class FwdCmd(DataList):
    """In-band ``MemRStream`` descriptor: forward ``fwd_len`` words verbatim, then burst ``len`` words
    read from ``addr``.

    The relayed bursts are **opaque** — they are the downstream consumer's descriptor + payload, and
    the reader never parses them.  That is what makes one reader serve any application (memcpy: a write
    descriptor; poly: coefficients).

    Relaying by **burst** (not word count) is what preserves opacity: the reader cannot know where the
    consumer's descriptor ends and its payload begins, so it relays packet boundaries instead.  That is
    why the in-band streams set ``has_tlast=True`` — a whole-burst read is only defined for a
    packet-delimited stream (``StreamIFSlave.get()`` refuses a countless read otherwise)."""
    cpp_repr: ClassVar[str] = "FwdCmd"
    include_filename: ClassVar[str] = "fwd_cmd.h"

    elements = {
        "addr":        {"schema": Word32, "description": "element/word offset to read from"},
        "len":         {"schema": Word32, "description": "number of packed words to read"},
        "fwd_bursts":  {"schema": Word32,
                        "description": "opaque bursts to relay verbatim BEFORE the data"},
    }


class WrCmd(DataList):
    """In-band ``MemWStream`` descriptor: buffer the next ``xfer_len`` payload words, then drain ``len``
    data words and pure-write them at ``addr``.  The payload is echoed on completion."""
    cpp_repr: ClassVar[str] = "WrCmd"
    include_filename: ClassVar[str] = "wr_cmd.h"

    elements = {
        "addr":     {"schema": Word32, "description": "element/word offset to write at"},
        "len":      {"schema": Word32, "description": "number of packed data words to write"},
        "xfer_len": {"schema": Word32, "description": "opaque payload words following this descriptor"},
    }


class WrComplete(DataList):
    """In-band completion header: ``len`` words written, then ``xfer_len`` echoed payload words follow
    it **in-band** on ``s_done`` (the same length-prefixed framing as the input)."""
    cpp_repr: ClassVar[str] = "WrComplete"
    include_filename: ClassVar[str] = "wr_complete.h"

    elements = {
        "len":      {"schema": Word32, "description": "number of data words written"},
        "xfer_len": {"schema": Word32, "description": "echoed payload words that follow this header"},
    }


#: The (shared, cached) ``xfer_msg`` array schema at the default ``max_xfer_len=8`` — needed as its
#: own :class:`~waveflow.hw.dataschema.DataSchemaStep` entry (a ``DataArray`` gen-includes its own
#: header; ``MRCmd``/``MWCmd``/``MemComplete``'s headers only ``#include`` it, they don't emit it).
XferMsgArr = MRCmd.elements["xfer_msg"]["schema"]


@dataclass(frozen=True)
class KernelTask:
    """The fixed ``hls::task`` body descriptor a composable component exposes for the composite
    codegen (:func:`~examples.mem_copy.mem_copy.composite_top_spec`).

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
    #: **In-band framing** (``plans/memstream_inband.md``).  When ``True`` the command stream carries
    #: ``[FwdCmd | fwd_len opaque words]`` and this component forwards those words verbatim to
    #: ``m_out`` before the data burst — a pure pass-through that never parses them, so one reader
    #: serves any consumer.  Default ``False`` keeps the legacy :class:`MRCmd` protocol.
    inband: HwParam[bool] = False
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self._cmd_cls = MRCmd.specialize(max_xfer_len=int(self.max_xfer_len))
        self._complete_cls = MemComplete.specialize(max_xfer_len=int(self.max_xfer_len))
        # m_mem is the sole read owner, and the TYPE says so: a stray write is an AttributeError in
        # the model and a compile error in the generated C++ (const pointer + #pragma HLS stable),
        # both derived from this one declaration rather than restated in a codegen table.
        # See plans/endpoint_types_not_tags.md.
        self.m_mem = MMIFReadMaster(
            name=f"{self.name}_m_mem", sim=self.sim, bitwidth=int(self.mem_dwidth))
        # In-band mode relays OPAQUE bursts, which is only defined on a packet-delimited stream.
        self.s_cmd = StreamIFSlave(
            name=f"{self.name}_s_cmd", sim=self.sim, bitwidth=int(self.mem_dwidth),
            has_tlast=bool(self.inband))
        self.m_out = StreamIFMaster(
            name=f"{self.name}_m_out", sim=self.sim, bitwidth=int(self.mem_dwidth),
            has_tlast=bool(self.inband))
        eps = [self.m_mem, self.s_cmd, self.m_out]
        if self.emit_done:
            self.s_done = StreamIFMaster(
                name=f"{self.name}_s_done", sim=self.sim, bitwidth=int(self.mem_dwidth),
                has_tlast=bool(self.inband))
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
        if self.inband:
            # In-band/framed reader (plans/memcopy_inband_integration.md): reads a FwdCmd off the framed
            # s_cmd, relays the opaque prefix, fetches src data.  Same 3-arg shape, framed_word edges.
            return KernelTask(
                "mem_r_stream_framed_task", "mem_r_stream_framed_task.h", ("s_cmd", "m_mem", "m_out"),
                template_args=(int(self.mem_dwidth),))
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
        if self.inband:
            yield from self._run_iter_inband()
            return
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

    def _run_iter_inband(self) -> ProcessGen[None]:
        """One framed transfer: forward the opaque prefix, then burst the data (``inband=True``).

        Reads ``[FwdCmd | fwd_len words]`` off ``s_cmd`` and emits ``[those words | data]`` on
        ``m_out``.  The forwarded words are **never parsed** — they are the downstream consumer's
        descriptor + payload — and they are streamed straight through, so this component needs no
        buffer at all (only the consumer, which must hold the payload across the data phase, does).
        """
        cmd = yield from self.s_cmd.get(FwdCmd)
        t_start = self.now
        # Relay each opaque burst whole, boundary included.  A countless get() returns the entire
        # burst -- the only read that does not require knowing the contents, which is exactly what
        # "do not parse the payload" demands.
        for _ in range(int(cmd.fwd_bursts)):
            burst = yield from self.s_cmd.get()
            yield from self.m_out.write(np.asarray(burst))
        w0, nw = int(cmd.addr), int(cmd.len)
        region = self.m_mem.region(self._base, self._word_t, word_bw=self._mem_bw)
        words, t0 = yield from region.read_slice_pipelined(w0, w0 + nw)
        yield from self.m_out.write_pipelined(words, t_out_start=t0 + self._fill)
        self.transfer_spans.append(self.now - t_start)


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
    #: **In-band framing** (``plans/memstream_inband.md``).  When ``True`` there is **no ``s_cmd``**:
    #: ``s_in`` carries ``[WrCmd | xfer_len payload words | len data words]``, so a descriptor can
    #: never be paired with the wrong data.  ``max_xfer_len`` then bounds the local payload **buffer**
    #: (this component must hold the payload across the data phase to echo it), not the protocol —
    #: the wire cost is the per-instance ``xfer_len``.  Default ``False`` keeps the legacy protocol.
    inband: HwParam[bool] = False
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self._cmd_cls = MWCmd.specialize(max_xfer_len=int(self.max_xfer_len))
        self._complete_cls = MemComplete.specialize(max_xfer_len=int(self.max_xfer_len))
        # The sole write owner — the type declares it (see MemRStream.m_mem above).
        self.m_mem = MMIFWriteMaster(
            name=f"{self.name}_m_mem", sim=self.sim, bitwidth=int(self.mem_dwidth))
        if not self.inband:
            self.s_cmd = StreamIFSlave(
                name=f"{self.name}_s_cmd", sim=self.sim, bitwidth=int(self.mem_dwidth),
                has_tlast=False)
        self.s_in = StreamIFSlave(
            name=f"{self.name}_s_in", sim=self.sim, bitwidth=int(self.mem_dwidth),
            has_tlast=bool(self.inband))
        # In-band: the descriptor rides on s_in, so there is no separate command port.
        eps = [self.m_mem, self.s_in] if self.inband else [self.m_mem, self.s_cmd, self.s_in]
        if self.emit_done:
            self.s_done = StreamIFMaster(
                name=f"{self.name}_s_done", sim=self.sim, bitwidth=int(self.mem_dwidth),
                has_tlast=bool(self.inband))
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
        if self.inband:
            # In-band/framed writer (plans/memcopy_inband_integration.md): NO s_cmd -- the WrCmd rides
            # in-band on the single framed s_in ([WrCmd | payload | data]).  The payload buffer needs a
            # compile-time bound, so max_xfer_len is a SECOND template arg (mem_w_stream_framed_done_task
            # <MEM_DW, MAX_XFER>).  emit_done is implied (the composite always echoes completion).
            return KernelTask(
                "mem_w_stream_framed_done_task", "mem_w_stream_framed_done_task.h",
                ("s_in", "m_mem", "s_done"),
                template_args=(int(self.mem_dwidth), int(self.max_xfer_len)))
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
        if self.inband:
            yield from self._run_iter_inband()
            return
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

    def _run_iter_inband(self) -> ProcessGen[None]:
        """One framed transfer off ``s_in`` (``inband=True``) — the four-step state machine.

        ``[WrCmd | xfer_len payload words | len data words]``: read the descriptor, buffer the opaque
        payload, drain and pure-write the data, then echo ``[WrComplete | payload]``.  The descriptor
        and its data are contiguous on one stream, so they cannot be mispaired — that is the point.
        The payload is buffered (not streamed through) precisely because it is echoed *after* the
        write; ``max_xfer_len`` bounds that buffer.
        """
        cmd = yield from self.s_in.get(WrCmd)
        n_x = int(cmd.xfer_len)
        if n_x > int(self.max_xfer_len):
            raise ValueError(
                f"{self.name}: xfer_len {n_x} exceeds the payload buffer bound max_xfer_len="
                f"{int(self.max_xfer_len)} — raise max_xfer_len or shorten the payload.")
        t_start = self.now
        payload = None
        if n_x:
            # Whole-burst read, then cross-check against the descriptor's xfer_len.  The burst
            # boundary and the length prefix verify each other -- a producer that framed the payload
            # wrongly fails HERE, loudly, instead of silently shifting the data that follows.
            payload = yield from self.s_in.get()
            if len(payload) != n_x:
                raise ValueError(
                    f"{self.name}: payload burst carried {len(payload)} words but the descriptor "
                    f"declared xfer_len={n_x} — producer framing is inconsistent.")
        w0, nw = int(cmd.addr), int(cmd.len)
        words, t0 = yield from self.s_in.get_pipelined(self._word_t, count=nw)
        region = self.m_mem.region(self._base, self._word_t, word_bw=self._mem_bw)
        yield from region.write_slice_pipelined(
            w0, words, t_out_start=t0 + self._fill, element_type=self._word_t)
        self.transfer_spans.append(self.now - t_start)
        if self.emit_done:
            yield from self.s_done.write(WrComplete(len=nw, xfer_len=n_x))
            if n_x:
                yield from self.s_done.write(np.asarray(payload))
