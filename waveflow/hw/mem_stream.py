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

import math
from dataclasses import dataclass, field
from pathlib import Path
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
# In-band framed descriptors (``inband=True``) — see plans/memcopy_inband_integration.md
#
# The framed alternative to the two-stream (s_cmd + s_in) protocol is a symmetric FORWARDING onion:
# each stage reads the one descriptor addressed to it, does its work, and relays the following bursts
# opaquely.  ``MemRCmd`` and ``MemWCmd`` are the SAME shape — an address, a length, and ``fwd_bursts``
# (how many following bursts this stage relays).  A composite frames one stream carrying
# ``[MemRCmd | MemWCmd | response... ]``; the reader strips ``MemRCmd`` (relay ``fwd_bursts`` bursts,
# then append the data it read), the writer strips ``MemWCmd`` (buffer ``fwd_bursts`` bursts across the
# write, then emit them on ``s_done``).  Neither mem-stream constructs a typed message — the ONLY
# schema-aware component is the composite's sequencer.  The relayed bursts are opaque (a whole-burst
# read needs ``has_tlast=True``: ``StreamIFSlave.get()`` refuses a countless read on an unframed
# stream), so one reader/writer serves any application.
# ---------------------------------------------------------------------------------------------


class MemRCmd(DataList):
    """In-band ``MemRStream`` descriptor: relay ``fwd_bursts`` opaque bursts, then burst ``len`` words
    read from ``addr`` (appended after the relayed prefix)."""
    cpp_repr: ClassVar[str] = "MemRCmd"
    include_filename: ClassVar[str] = "mem_r_cmd.h"

    elements = {
        "addr":       {"schema": Word32, "description": "element/word offset to read from"},
        "len":        {"schema": Word32, "description": "number of packed words to read"},
        "fwd_bursts": {"schema": Word32,
                       "description": "opaque bursts to relay verbatim BEFORE the data"},
    }


class MemWCmd(DataList):
    """In-band ``MemWStream`` descriptor: buffer the next ``fwd_bursts`` opaque bursts, drain ``len``
    data words and pure-write them at ``addr``, then emit the buffered bursts on ``s_done``.  Same shape
    as :class:`MemRCmd` — the forwarding protocol is symmetric."""
    cpp_repr: ClassVar[str] = "MemWCmd"
    include_filename: ClassVar[str] = "mem_w_cmd.h"

    elements = {
        "addr":       {"schema": Word32, "description": "element/word offset to write at"},
        "len":        {"schema": Word32, "description": "number of packed data words to write"},
        "fwd_bursts": {"schema": Word32,
                       "description": "opaque bursts to buffer across the write, then echo on s_done"},
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

#: HLS ``max_read_burst_length`` / ``max_write_burst_length`` — a contiguous transfer of ``n`` words
#: is issued as ``ceil(n / MEM_AXI_MAX_BURST)`` bursts.  This is ``num_trans``, the timing feature the
#: RTL law moves on (see ``plans/memcpy_timing_calibration.md``); it must match
#: ``ExtractBurstsStep.max_burst_len``.
MEM_AXI_MAX_BURST = 16


def _word_type(mem_dwidth: int) -> type[IntField]:
    """The packed-memory word element type — one ``ap_uint<MEM_DW>`` word.  Used as the Region
    element type so an element coordinate **is** a word index (``nwords_per_inst == 1``)."""
    return IntField.specialize(bitwidth=int(mem_dwidth), signed=False)


def _resolve_calib_dir(calib_dir, platform_dir, component: str) -> "Path | None":
    """Where a mem-stream's :class:`~waveflow.calib.timing_model.StreamTimingModel` corpus + params
    live, or ``None`` (uncalibrated).  Two ways to point it, checked in precedence:

    * an explicit ``calib_dir`` — the low-level override (a project-local one-off directory), or
    * a ``platform_dir`` — the shared platform library, where the residual is stored **keyed by the
      component identity** (:meth:`~waveflow.calib.platform.PlatformCalib.component_dir`) so it reloads
      in any project composing this same infra component on the same platform, no recalibration.

    Neither set → ``None`` → no model attached, pysim timing exactly as before."""
    if calib_dir is not None:
        return Path(calib_dir)
    if platform_dir is not None:
        from waveflow.calib.platform import PlatformCalib
        return PlatformCalib(platform_dir).component_dir(component)
    return None


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
    #: **In-band framing** (``plans/memcopy_inband_integration.md``).  When ``True`` the command stream
    #: carries a :class:`MemRCmd`, and this component relays its ``fwd_bursts`` opaque bursts verbatim to
    #: ``m_out`` before the data burst — a pure pass-through that never parses them, so one reader
    #: serves any consumer.  Default ``False`` keeps the legacy :class:`MRCmd` protocol.
    inband: HwParam[bool] = False
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    #: Timing calibration, mirroring :class:`MemWStream` — this reader is infra too, so its control
    #: residual is a ``(component, platform)`` property.  ``calib_dir`` is the project-local override;
    #: :attr:`platform_dir` is the shared platform library (stored under
    #: ``<platform_dir>/components/<task-body>/``).  Both ``None`` (default) → uncalibrated: no model,
    #: no delay, pysim timing exactly as before.  See :func:`_resolve_calib_dir`.
    calib_dir: "str | Path | None" = None
    platform_dir: "str | Path | None" = None

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
        #: Per-firing ``(start, end)`` cycle windows — the activity-diagram / instrumentation view.
        self.fire_log: list[tuple[float, float]] = []
        comp_id = self.kernel_task().task_fn
        resolved = _resolve_calib_dir(self.calib_dir, self.platform_dir, comp_id)
        if resolved is not None:
            from waveflow.calib.timing_model import StreamTimingModel
            self.add_timing_model(StreamTimingModel(
                component=comp_id, calib_dir=resolved, clk=self.clk))

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
            # In-band/framed reader (plans/memcopy_inband_integration.md): reads a MemRCmd off the framed
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
        words, t0 = yield from region.read_slice_pipelined(
            w0, w0 + nw, num_trans=math.ceil(nw / MEM_AXI_MAX_BURST))
        # early-anchor the output at the first-word-available time + pipeline fill: the read and
        # write OVERLAP (write_pipelined shortens its wait when the anchor is already past).
        yield from self.m_out.write_pipelined(words, t_out_start=t0 + self._fill)
        self.transfer_spans.append(self.now - t_start)
        self.fire_log.append((t_start / self.clk.period, self.now / self.clk.period))
        if self.emit_done:
            complete = self._complete_cls(len=nw, xfer_len=int(cmd.xfer_len), xfer_msg=cmd.xfer_msg)
            yield from self.s_done.write(complete)
        yield from self._timed_tail(nw)

    def _timed_tail(self, nw: int) -> ProcessGen[None]:
        """The reader's trailing calibration delay — its own control residual, mirroring the writer's
        posted-write drain.  A no-op (records nothing, waits nothing) when uncalibrated; the current
        firing's predicted delay once a model is attached (see :meth:`FreeRunComp.timed_delay`)."""
        dly = self.timed_delay({"nwords": nw, "num_trans": math.ceil(nw / MEM_AXI_MAX_BURST)})
        if dly:
            yield self.timeout(dly)

    def _run_iter_inband(self) -> ProcessGen[None]:
        """One framed transfer: relay the opaque prefix, then burst the data (``inband=True``).

        Reads a :class:`MemRCmd` off ``s_cmd``, relays its ``fwd_bursts`` opaque bursts verbatim, then
        emits the ``len`` words it read — ``[relayed bursts | data]`` on ``m_out``.  The relayed bursts
        are **never parsed** (they are the downstream stages' descriptor + response), so this component
        needs no buffer at all and serves any application.
        """
        cmd = yield from self.s_cmd.get(MemRCmd)
        t_start = self.now
        # Relay each opaque burst whole, boundary included.  A countless get() returns the entire
        # burst -- the only read that does not require knowing the contents, which is exactly what
        # "do not parse the payload" demands.
        for _ in range(int(cmd.fwd_bursts)):
            burst = yield from self.s_cmd.get()
            yield from self.m_out.write(np.asarray(burst))
        w0, nw = int(cmd.addr), int(cmd.len)
        region = self.m_mem.region(self._base, self._word_t, word_bw=self._mem_bw)
        words, t0 = yield from region.read_slice_pipelined(
            w0, w0 + nw, num_trans=math.ceil(nw / MEM_AXI_MAX_BURST))
        yield from self.m_out.write_pipelined(words, t_out_start=t0 + self._fill)
        self.transfer_spans.append(self.now - t_start)
        self.fire_log.append((t_start / self.clk.period, self.now / self.clk.period))
        yield from self._timed_tail(nw)


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
    max_xfer_len: HwParam[int] = 8     # LEGACY xfer_msg capacity — bridged into MWCmd/MemComplete's Param
    #: **In-band only**: bound on the local forward **buffer** — the total words this component holds
    #: across the write while relaying the response bursts (the C++ body's fixed ``fwd[MAX_FWD]``
    #: array).  A bound on the *buffer*, not the protocol.  Distinct from :attr:`max_xfer_len`, which is
    #: the legacy two-stream ``xfer_msg`` array capacity — the two meant the same number once and no
    #: longer do, so they are separate knobs.
    max_fwd_words: HwParam[int] = 8
    #: When ``True`` the endpoint also exposes an ``s_done`` :class:`StreamIFMaster` and emits one
    #: :class:`MemComplete` echo (words written + the command's ``xfer_msg`` cookie) per job — the
    #: composition variant used by the ``MemCopy`` composite (its fixed body is
    #: ``mem_w_stream_done_task``).  Default ``False`` keeps the standalone Gate-1 kernel (3-arg
    #: ``mem_w_stream_task``) unchanged.
    emit_done: bool = False
    #: **In-band framing** (``plans/memcopy_inband_integration.md``).  When ``True`` there is **no
    #: ``s_cmd``**: ``s_in`` carries ``[MemWCmd | fwd_bursts opaque bursts | len data words]``, so a
    #: descriptor can never be paired with the wrong data.  :attr:`max_fwd_words` then bounds the local
    #: forward buffer (this component holds the response bursts across the write, then echoes them).
    #: Default ``False`` keeps the legacy protocol.
    inband: HwParam[bool] = False
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    #: When set, attach a :class:`~waveflow.calib.timing_model.StreamTimingModel` at this directory
    #: and inject its predicted (trailing) delay per firing — the posted-write drain the RTL law
    #: captures.  ``None`` (default) leaves the component uncalibrated: no model, no delay, and the
    #: pysim timing is exactly as before.  A model with an unfitted / zero-seed predicts 0, so
    #: attaching one only records firings (for collection) until ``params.json`` exists.  A project-local
    #: override of :attr:`platform_dir` — set at most one; ``calib_dir`` wins if both are.
    calib_dir: "str | Path | None" = None
    #: **Shared** calibration home: this reusable writer's residual is a property of ``(component,
    #: platform)``, so pointing ``platform_dir`` at the platform library stores/loads it under
    #: ``<platform_dir>/components/<task-body>/`` — reused by any accelerator on the same platform
    #: without recalibrating (:class:`~waveflow.calib.platform.PlatformCalib`).  Prefer this over
    #: ``calib_dir`` for the framework components; ``calib_dir`` remains the one-off/local override.
    platform_dir: "str | Path | None" = None

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
        #: Per-firing ``(start, end)`` cycle windows — the activity-diagram / instrumentation view.
        self.fire_log: list[tuple[float, float]] = []
        # `component` must match the id this stream's firings carry in the RTL timing table — the
        # task-body name, which kernel_task() already knows — and it keys the shared platform library.
        comp_id = self.kernel_task().task_fn
        resolved = _resolve_calib_dir(self.calib_dir, self.platform_dir, comp_id)
        if resolved is not None:
            from waveflow.calib.timing_model import StreamTimingModel
            self.add_timing_model(StreamTimingModel(
                component=comp_id, calib_dir=resolved, clk=self.clk))

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
            # In-band/framed writer (plans/memcopy_inband_integration.md): NO s_cmd -- the MemWCmd rides
            # in-band on the single framed s_in ([MemWCmd | response bursts | data]).  The forward buffer
            # needs a compile-time bound, so max_fwd_words is a SECOND template arg
            # (mem_w_stream_framed_done_task<MEM_DW, MAX_FWD>).  emit_done is implied here.
            return KernelTask(
                "mem_w_stream_framed_done_task", "mem_w_stream_framed_done_task.h",
                ("s_in", "m_mem", "s_done"),
                template_args=(int(self.mem_dwidth), int(self.max_fwd_words)))
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
            w0, words, t_out_start=t0 + self._fill, element_type=self._word_t,
            num_trans=math.ceil(nw / MEM_AXI_MAX_BURST))
        self.transfer_spans.append(self.now - t_start)
        self.fire_log.append((t_start / self.clk.period, self.now / self.clk.period))
        if self.emit_done:
            complete = self._complete_cls(len=nw, xfer_len=int(cmd.xfer_len), xfer_msg=cmd.xfer_msg)
            yield from self.s_done.write(complete)

    def _run_iter_inband(self) -> ProcessGen[None]:
        """One framed transfer off ``s_in`` (``inband=True``) — the forwarding writer.

        ``[MemWCmd | fwd_bursts opaque bursts | len data words]``: read the descriptor, **buffer** the
        opaque bursts (they are the response, and must not go out before the data is stored), drain and
        pure-write the ``len`` data words, then emit the buffered bursts on ``s_done``.  The writer
        never parses what it forwards — it constructs no completion of its own; the composite's
        sequencer framed the response.  ``max_fwd_words`` bounds the buffer.
        """
        cmd = yield from self.s_in.get(MemWCmd)
        n_fwd = int(cmd.fwd_bursts)
        t_start = self.now
        # Buffer the opaque bursts to relay AFTER the write.  Whole-burst reads (the writer forwards
        # packet boundaries it does not understand); held across the store so the response lands last.
        # max_fwd_words bounds the total buffered words (the C++ body's fixed fwd[MAX_FWD] array).
        fwd = []
        n_buf = 0
        for _ in range(n_fwd):
            burst = yield from self.s_in.get()
            n_buf += len(burst)
            if n_buf > int(self.max_fwd_words):
                raise ValueError(
                    f"{self.name}: forwarded {n_buf} words exceeds the buffer bound max_fwd_words="
                    f"{int(self.max_fwd_words)} — raise max_fwd_words or shorten the response.")
            fwd.append(np.asarray(burst))
        w0, nw = int(cmd.addr), int(cmd.len)
        words, t0 = yield from self.s_in.get_pipelined(self._word_t, count=nw)
        region = self.m_mem.region(self._base, self._word_t, word_bw=self._mem_bw)
        yield from region.write_slice_pipelined(
            w0, words, t_out_start=t0 + self._fill, element_type=self._word_t,
            num_trans=math.ceil(nw / MEM_AXI_MAX_BURST))
        self.transfer_spans.append(self.now - t_start)
        self.fire_log.append((t_start / self.clk.period, self.now / self.clk.period))
        if self.emit_done:
            for burst in fwd:
                yield from self.s_done.write(burst)
        # Trailing calibration delay: the posted-write drain the RTL keeps running after s_done, so
        # the firing is not really done -- and the next cannot start -- until it completes.  A no-op
        # (0, but still records the firing) when uncalibrated; see FreeRunComp.timed_delay.
        # `n_fwd` (the forwarded/echoed bursts) is a feature too: buffering and echoing them costs
        # cycles that scale with the count.  A model whose basis omits n_fwd ignores the key; one that
        # includes it (a fixture that swept it) charges the per-message cost.
        dly = self.timed_delay(
            {"nwords": nw, "num_trans": math.ceil(nw / MEM_AXI_MAX_BURST), "n_fwd": n_fwd})
        if dly:
            yield self.timeout(dly)
