"""fir_block.py — the block FIR: the first example whose module carries **state across firings**.

The open deliverable of ``plans/add_state.md``.  ``add_state`` (Stages 1-3) is built and XSI-gated on
``examples/state_toy``; this is the design it was built *for*, and it is the first example that needs
**two distinct flavours of state in one module**, so no single-flavour toy can stand in for it:

1. **Load-once, held** — a ``LOAD_TAPS`` command fills the coefficient store, which survives every
   later firing.
2. **Per-block carry** — a ``FILTER`` command streams a block through, and the tail (the last ``T-1``
   samples) is kept as the *next* block's initial condition.  The command can also select **zeros
   instead of the carry** (``zero_state``), so the state has a documented reset path that is part of
   the design rather than an artifact of ``ap_rst``.

Topology (``plans/add_state.md`` decision 2) — a composite, because addresses mean ``m_axi`` and a
task body cannot own one::

    s_cmd -> [ fir_cmd_rx ] -> [ MemRStream ] -> [ fir_compute ] -> [ MemWStream ] -> s_done
                                                       ^ taps + carry, as add_state

**One compute leaf handles both opcodes.**  They arrive over the same read stream, opcode-tagged in
the framed descriptor; :meth:`FirCompute.run_iter` dispatches on it, writing ``taps`` on a load firing
and reading it on a filter firing.  There is deliberately **no overlap** between loading job *n*'s taps
and computing job *n-1*: two firings of one task, strictly ordered by the command stream.  Overlap is
the one requirement that would force the taps to stop being state and become a channel — analysed and
deferred in the plan.

**The no-output firing.**  ``LOAD_TAPS`` consumes input and produces no data, which is the plan's
flagged deadlock risk (this codebase has been bitten twice by a stage that reads without emitting).
The resolution here is that the firing is *not* silent: it still frames a ``MemWCmd``, just with
``len=0`` and ``fwd_bursts=1``.  The writer's ``S2A`` loop then trips zero times — no AXI transaction
at all — while the ``ECHO`` loop still emits the descriptor on ``s_done``.  Every job produces exactly
one completion, and the token path is uniform across both opcodes.

Fixed point
-----------
Samples, coefficients, and output share **one** ``FixedField`` format; the accumulator is
full-precision and *derived*, not hand-sized: :func:`~waveflow.hw.fixpoint.mult` gives
``<Wa+Wb, Ia+Ib>`` and :func:`~waveflow.hw.fixpoint.fixed_sum` grows the integer bits by
``ceil(log2 T)``.  A ``for``-loop of ``add`` would grow ``I`` by *one bit per tap* (``+T``) instead of
``+ceil(log2 T)`` — so the window reduction is ``fixed_sum``, never repeated ``add``.
:func:`~waveflow.hw.fixpoint.quantize` back to the sample format is the single declared lossy step.

**Transport is packed**, ``LW = max(1, MEM_DW // W)`` samples per word, and neither side hand-rolls it:
the C++ uses the generated ``<stem>_array_utils`` lane routines and the Python twin uses
``DataArray.serialize`` — one packing contract, so a numpy golden and a Vitis kernel agree bit-for-bit
at any ``LW``.  See ``docs/guide/vectorization/hls/arrayutils.md``; if a kernel here ever grows a
``.range()`` to pull elements out of a word, that is the bug, not the idiom.

The packing factor is integer division, so an awkward width is not a problem: ``W=18`` gives ``LW=1``
(one sample per word, 14 bits unused), ``W=16`` gives 2, ``W=8`` gives 4.  A ``n``-sample block is
therefore ``ceil(n/LW)`` words — the commands and descriptors carry **samples**, the mem-streams carry
**words**, and :func:`nwords` is the single conversion between them.

Two realizations, one golden
----------------------------
``unroll_lane`` (an :class:`HwParam`) picks between two hand-written task bodies that compute
*bit-identical* results and differ only in physics — see :attr:`FirCompute.unroll_lane` for the
measured table.  ``False`` (``fir_compute_serial_task.h``) holds throughput at one sample per cycle and
lets the multiplier count fall with precision; ``True`` (``fir_compute_unroll_task.h``) consumes a
whole lane per beat, buying throughput at a fixed ``LW*NTAP`` multipliers.  That pairing is what makes
this example a monomorphized-QoR probe rather than one design with a knob.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import ClassVar

import numpy as np

from waveflow.build.composite_gen import INCLUDE_DIR
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataArray, DataList, EnumField, IntField
from waveflow.hw.fixpoint import FixedField, fixed_sum, mult, quantize
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.hw_state import HwState
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.mem_stream import KernelTask, MemRCmd, MemRStream, MemWCmd, MemWStream
from waveflow.hw.synth import sim_only, synthesizable
from waveflow.simulation.simobj import ProcessGen

HERE = Path(__file__).resolve().parent

#: The transport word.  One sample per word (see the module docstring), so the memory word width is
#: independent of the sample width and the sweep moves exactly one thing.
MEM_DW = 32

#: Defaults: 16-bit samples with 2 integer bits (Q2.14) and a 32-tap filter.
DEFAULT_SAMP_W = 16
DEFAULT_SAMP_I = 2
DEFAULT_NTAP = 32

Word32 = IntField.specialize(bitwidth=32, signed=False)


class FirOp(IntEnum):
    """The two opcodes one compute leaf dispatches on."""

    LOAD_TAPS = 0
    FILTER = 1


FirOpField = EnumField.specialize(enum_type=FirOp, bitwidth=32)


class FirCmd(DataList):
    """One host command on the boundary ``s_cmd`` (a plain word stream).

    ``n`` is a **sample** count: the tap count for ``LOAD_TAPS``, the block length for ``FILTER``.
    Offsets are word coordinates (one sample per word), the addressing convention everywhere here."""

    include_filename: ClassVar[str | None] = "fir_cmd.h"
    elements = {
        "op":         {"schema": FirOpField, "description": "LOAD_TAPS or FILTER"},
        "src_off":    {"schema": Word32, "description": "source word offset (taps, or the block)"},
        "n":          {"schema": Word32, "description": "sample count (tap count, or block length)"},
        "dst_off":    {"schema": Word32, "description": "destination word offset (FILTER only)"},
        "zero_state": {"schema": Word32, "description": "FILTER: 1 = start from zeros, not the carry"},
        "tx_id":      {"schema": Word32, "description": "host transaction ID, echoed on completion"},
    }


class FirDesc(DataList):
    """The framed internal descriptor, relayed opaquely by the mem-streams and echoed on ``s_done``.

    Carries what the *downstream* stages need — the opcode to dispatch on, the runtime length (so the
    RTL is scenario-independent rather than baking a block size), the destination, the reset select,
    and the transaction id the host matches its completion against."""

    include_filename: ClassVar[str | None] = "fir_desc.h"
    elements = {
        "op":         {"schema": FirOpField, "description": "LOAD_TAPS or FILTER"},
        "n":          {"schema": Word32, "description": "sample count for this job"},
        "dst_off":    {"schema": Word32, "description": "destination word offset"},
        "zero_state": {"schema": Word32, "description": "1 = start this block from zeros"},
        "tx_id":      {"schema": Word32, "description": "echoed transaction id"},
    }


#: Schema classes the gen-include step emits C++ headers for.  ``FirCmd`` is the PLAIN boundary
#: command; the rest ride framed edges, so their headers also need the ``framed_word`` accessors.
#: ``FirOpField`` is listed in its own right (poly's ``PolyCmdTypeField`` precedent): the opcode has to
#: reach C++ as a real ``enum``, which is what lets the task body dispatch on ``FirOp::LOAD_TAPS``
#: rather than on a bare integer that nothing checks.
SCHEMA_CLASSES = [FirOpField, FirCmd, FirDesc, MemRCmd, MemWCmd]
FRAMED_SCHEMAS = frozenset({FirDesc, MemRCmd, MemWCmd})


# --- format helpers ---------------------------------------------------------------------------


def samp_type(samp_w: int = DEFAULT_SAMP_W, samp_i: int = DEFAULT_SAMP_I) -> type[FixedField]:
    """The one sample/coefficient/output format — ``ap_fixed<W, I>``, signed.

    Built per-instance (the width is an :class:`HwParam`), which is why the hook's state arguments
    resolve their concrete type from the *registered instance* rather than from an annotation
    (``plans/add_state.md`` decision 5).

    ``include_dir`` is what puts the element's generated ``<stem>_array_utils.h`` next to the other
    headers instead of at the example root — the same thing the interleaver's ``IlElem`` declares."""
    return FixedField.specialize(W=int(samp_w), I=int(samp_i), signed=True,
                                 include_dir=INCLUDE_DIR)


def tap_array_type(ntap: int, samp_cls: type[FixedField]) -> type:
    """The coefficient store's schema.  ``cpp_storage="raw"`` is what lowers it to a bare
    ``Samp taps[T]`` — the form a ``static`` declaration wants."""
    return DataArray.specialize(samp_cls, max_shape=(int(ntap),), cpp_storage="raw")


def carry_array_type(ntap: int, samp_cls: type[FixedField]) -> type:
    """The per-block carry: the last ``T-1`` samples of the previous block."""
    return DataArray.specialize(samp_cls, max_shape=(max(int(ntap) - 1, 1),), cpp_storage="raw")


def lane_width(mem_dwidth: int, samp_w: int) -> int:
    """``LW`` — samples per transport word, the Python twin of the generated
    ``<stem>_array_utils::lane_capacity<MEM_DW>()``: ``max(1, MEM_DW // W)``.

    This is *derived*, not chosen.  An earlier cut of this example asserted one sample per 32-bit word
    on the grounds that packing "is not expressible" at an awkward width like ``W=18`` — which is
    wrong: the packing factor is integer division, so ``W=18`` simply gives ``pf=1`` and lands on the
    one-per-word case anyway, while ``W=16`` packs two and ``W=8`` packs four."""
    return max(1, int(mem_dwidth) // int(samp_w))


def nwords(n: int, lw: int) -> int:
    """Transport words for ``n`` samples at ``LW`` samples per word."""
    return (int(n) + int(lw) - 1) // int(lw)


def pack_samples(stored, samp_cls: type[FixedField], word_bw: int) -> np.ndarray:
    """Stored sample values -> transport words, through the **framework** serializer.

    ``DataArray.serialize`` is the single source of the packing contract (LSB-first, ``pf`` elements
    per word) and the generated ``<stem>_array_utils`` C++ reads exactly that layout — so this is the
    twin of ``write_framed_stream_lane``, not a re-implementation of it.  Hand-rolling the shift and
    mask here is what makes a Python golden and a Vitis kernel disagree the moment ``LW > 1``."""
    arr = np.asarray(stored, dtype=np.int64).reshape(-1)
    cls = DataArray.specialize(samp_cls, max_shape=(max(len(arr), 1),))
    return np.asarray(cls(arr).serialize(word_bw=int(word_bw)), dtype=np.uint64)


def unpack_samples(words, n: int, samp_cls: type[FixedField], word_bw: int) -> np.ndarray:
    """Transport words -> ``n`` stored sample values; the inverse of :func:`pack_samples`."""
    cls = DataArray.specialize(samp_cls, max_shape=(max(int(n), 1),))
    got = cls().deserialize(np.asarray(words, dtype=np.uint64), word_bw=int(word_bw))
    return np.asarray(got, dtype=np.int64).reshape(-1)[:int(n)]


def _as_fixed(stored, samp_cls: type[FixedField]) -> DataArray:
    """Wrap stored integers as a ``DataArray[samp_cls]`` so the fixed-point free functions apply."""
    arr = np.asarray(stored)
    return DataArray.specialize(samp_cls, max_shape=arr.shape)(arr)


# --- the framer -------------------------------------------------------------------------------


@dataclass
class FirCmdRx(FreeRunMod):
    """Framer (mem_copy's ``Sequencer`` role): read one :class:`FirCmd` and frame the reader's command
    stream as ``[MemRCmd | FirDesc]`` — **one** read per job, with the descriptor relayed as a header
    ahead of the data (``fwd_bursts=1``).

    Both opcodes read: ``LOAD_TAPS`` fetches ``n`` coefficients, ``FILTER`` fetches an ``n``-sample
    block.  Uniform framing is what keeps the no-output opcode from needing a special path."""

    cpp_kernel_name: ClassVar[str | None] = "fir_cmd_rx"

    mem_dwidth: HwParam[int] = MEM_DW
    samp_w: HwParam[int] = DEFAULT_SAMP_W
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        self.lw = lane_width(w, self.samp_w)
        self.s_cmd = StreamIFSlave(name=f"{self.name}_s_cmd", sim=self.sim, bitwidth=w,
                                   has_tlast=False)          # plain host boundary
        self.cmd_out = StreamIFMaster(name=f"{self.name}_cmd_out", sim=self.sim, bitwidth=w,
                                      has_tlast=True)        # framed -> MemRStream
        for ep in (self.s_cmd, self.cmd_out):
            self.add_endpoint(ep)
        self.fire_log: list[tuple[float, float]] = []

    def kernel_task(self) -> KernelTask:
        """Overridden because this body is **hand-written** (``fir_cmd_rx_task.h``), like every other
        in-band framer in the tree: constructing descriptors and driving a ``framed_word`` channel is
        not in the extractor's vocabulary, so nothing can derive the name or the parameter order.
        ``run_iter`` below stays as the pysim golden.  See ``guide/comp_codegen/freerunning_override``."""
        return KernelTask("fir_cmd_rx_task", "fir_cmd_rx_task.h", ("s_cmd", "cmd_out"),
                          template_args=(int(self.mem_dwidth),))

    @sim_only
    def _mark_start(self) -> None:
        """Instrumentation only — a firing's start cycle, for the activity timeline."""
        self._t0 = self.now

    @sim_only
    def _log_firing(self) -> None:
        self.fire_log.append((self._t0 / self.clk.period, self.now / self.clk.period))

    def run_iter(self) -> ProcessGen[None]:
        w = int(self.mem_dwidth)
        cmd = yield from self.s_cmd.get(FirCmd)
        self._mark_start()
        desc = FirDesc(op=int(cmd.op), n=int(cmd.n), dst_off=int(cmd.dst_off),
                       zero_state=int(cmd.zero_state), tx_id=int(cmd.tx_id))
        # `n` is a SAMPLE count on the command; the reader speaks WORDS.  At LW samples per word that
        # is ceil(n/LW) -- the same conversion il_cmd_rx makes, and the reason the descriptor carries
        # `n` onward: only the schema-aware ends know the packing, the mem-streams relay opaquely.
        memr = MemRCmd(addr=int(cmd.src_off), len=nwords(int(cmd.n), self.lw), fwd_bursts=1)
        yield from self.cmd_out.write(np.asarray(memr.serialize(word_bw=w), dtype=np.uint64))
        yield from self.cmd_out.write(np.asarray(desc.serialize(word_bw=w), dtype=np.uint64))
        self._log_firing()


# --- the stateful compute ---------------------------------------------------------------------


@dataclass
class FirCompute(FreeRunMod):
    """The FIR itself, and **the reason this example exists**: the only module in the tree carrying two
    flavours of declared state.

    Reads the framed ``[FirDesc | data]`` off the reader and frames the writer's stream
    ``[MemWCmd | FirDesc | y]``.  Dispatches on the descriptor's opcode:

    * ``LOAD_TAPS`` — the ``n`` words are coefficients; they land in ``self.taps`` and stay there.
      Nothing is written back (``MemWCmd(len=0)``), but the descriptor still rides through to
      ``s_done``, so the job is completed like any other.
    * ``FILTER`` — the ``n`` words are a block.  ``y[i] = sum_k h[k]·x[i-k]``, where the ``k > i``
      terms come from ``self.carry`` (the previous block's tail) unless ``zero_state`` selects zeros.
      The new tail is written back to ``self.carry`` for the next block."""

    cpp_kernel_name: ClassVar[str | None] = "fir_compute"
    cpp_namespace: ClassVar[str | None] = "fir_compute_impl"

    mem_dwidth: HwParam[int] = MEM_DW
    ntap: HwParam[int] = DEFAULT_NTAP
    samp_w: HwParam[int] = DEFAULT_SAMP_W
    samp_i: HwParam[int] = DEFAULT_SAMP_I
    #: Which of the two realizations to emit.  ``False`` consumes ONE sample per iteration, carrying a
    #: lane index across iterations; ``True`` consumes a whole lane (``LW`` samples) per iteration with
    #: the taps unrolled ``LW`` ways.  The arithmetic is identical either way -- same ``acc_t``, same
    #: golden -- so this parameter changes only the *physics*, which is exactly what makes the pair a
    #: monomorphized-QoR probe rather than two designs.  Measured at ``T=32, MEM_DW=32``:
    #:
    #:   ============ ==== ===== ======== =========================
    #:   ``samp_w``   LW   II    DSP      throughput
    #:   ============ ==== ===== ======== =========================
    #:   24 (LW=1)    1    1     64       1 sample/cycle (identical)
    #:   16           2    1     32 / 64  1 / 2 samples per cycle
    #:   8            4    1     17 / 128 1 / 4 samples per cycle
    #:   ============ ==== ===== ======== =========================
    #:
    #: So ``False`` holds throughput constant and shrinks with precision (the DSP-packing story: two
    #: DSP48E1s per tap at W=24, one at W=16, two taps *sharing* a DSP at W=8), while ``True`` holds
    #: the multiplier count and buys throughput.  Both pipeline at II=1 -- the lane index costs 3
    #: cycles of latency, not initiation interval.
    unroll_lane: HwParam[bool] = False
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    calib_dir: "str | None" = None

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        t = int(self.ntap)
        self.samp_cls = samp_type(self.samp_w, self.samp_i)
        self.lw = lane_width(w, self.samp_w)

        self.s_in = StreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=w,
                                  has_tlast=True)             # framed <- MemRStream
        self.cmd_out = StreamIFMaster(name=f"{self.name}_cmd_out", sim=self.sim, bitwidth=w,
                                      has_tlast=True)         # framed -> MemWStream
        for ep in (self.s_in, self.cmd_out):
            self.add_endpoint(ep)

        # THE point of this example.  Two storages, two lifetimes, one module:
        #   taps  — written once by a LOAD_TAPS firing, read by every FILTER firing after it.
        #   carry — rewritten by every FILTER firing; the tail that makes block-wise filtering
        #           equal to filtering the whole signal.
        # `partition complete` on the taps because the window reduction reads all T every sample.
        self.taps = HwState(tap_array_type(t, self.samp_cls)(),
                            partition={"type": "complete"})
        self.carry = HwState(carry_array_type(t, self.samp_cls)())
        self.add_state(self.taps)
        self.add_state(self.carry)

        self.timing = self._build_timing_model()
        self.fire_log: list[tuple[float, float]] = []
        self.job_span_cyc: list[float] = []

    def _build_timing_model(self):
        """``cycles = latency + ii·n`` over the sample loop — seeded at II=1 (one output per cycle,
        the taps fully partitioned) with the pipeline depth as the intercept, and replaced by a fit
        when a calib dir is supplied.  Same shape as the interleaver's compute model."""
        from waveflow.calib.calib import LinCalibModel

        seed = {"n": FIR_COMPUTE_II_SEED, "intercept": FIR_COMPUTE_LATENCY_SEED}
        path = None if self.calib_dir is None else Path(self.calib_dir) / "params.json"
        model = LinCalibModel(basis=["n"], target="cycles", fit_intercept=True,
                              coeff_names=["n"], seed=seed, path=path)
        model.load_or_default()
        return model

    @property
    def variant(self) -> str:
        """``"unroll"`` or ``"serial"`` — the realization :attr:`unroll_lane` selects."""
        return "unroll" if bool(self.unroll_lane) else "serial"

    def kernel_task(self) -> KernelTask:
        """Hand-written body — see :meth:`FirCmdRx.kernel_task` for *why* hand-written.

        **The parameter picks the body by name.** ``unroll_lane`` selects between two separately-named
        task functions rather than one function with a compile-time branch inside.  Two reasons, and
        neither is Vitis's patchy appetite for ``if constexpr``: the variants need *different state
        shapes* (a ``dline[NTAP]`` shift register versus a ``dl[NTAP+LW-1]`` shared history), and one
        function would collapse into a single csynth report — throwing away the side-by-side QoR
        comparison that is the whole point of having both.

        Note what is *not* hand-written: the ``static`` storage.  Both bodies ``#include``
        ``fir_compute_state.inc``, which :func:`~waveflow.build.hwgen.state_decls_to_cpp` emits from
        the ``add_state`` registrations above — declarations, extents, element type, and the
        ``ARRAY_PARTITION`` pragma all single-sourced from Python.  So the storage cannot drift from
        the pysim twin even though the arithmetic around it is written by hand."""
        return KernelTask(f"fir_compute_{self.variant}_task",
                          f"fir_compute_{self.variant}_task.h", ("s_in", "cmd_out"),
                          template_args=(int(self.mem_dwidth),))

    @sim_only
    def _mark_start(self) -> None:
        self._t0 = self.now

    @sim_only
    def _compute_delay(self, n: int) -> float:
        """The fitted loop model's cost for an ``n``-sample block — a pysim latency model with no
        hardware meaning of its own (the RTL's cost is whatever the emitted loop schedules to).

        The **basis** is the loop's trip count, which is where the two realizations part company: the
        serial body trips once per sample and the unrolled body once per lane (``ceil(n/LW)`` beats).
        Both measured at II=1, so this is the throughput difference the parameter buys."""
        beats = nwords(n, self.lw) if bool(self.unroll_lane) else int(n)
        return max(0.0, float(self.timing.predict({"n": beats}))) * self.clk.period

    @sim_only
    def _log_firing(self) -> None:
        self.job_span_cyc.append((self.now - self._t0) / self.clk.period)
        self.fire_log.append((self._t0 / self.clk.period, self.now / self.clk.period))

    def run_iter(self) -> ProcessGen[None]:
        w = int(self.mem_dwidth)
        desc = yield from self.s_in.get(FirDesc)
        n = int(desc.n)
        nw = nwords(n, self.lw)          # the stream speaks WORDS; the descriptor carries SAMPLES
        data = yield from self.s_in.get(nwords_max=nw)
        self._mark_start()

        if int(desc.op) == FirOp.LOAD_TAPS:
            self.load_taps(np.asarray(data), n, self.taps)
            # No data written back -- but the job still completes.  len=0 makes the writer's S2A loop
            # trip zero times (no AXI transaction), while fwd_bursts=1 keeps the s_done echo.
            memw = MemWCmd(addr=int(desc.dst_off), len=0, fwd_bursts=1)
            yield from self.cmd_out.write(np.asarray(memw.serialize(word_bw=w), dtype=np.uint64))
            yield from self.cmd_out.write(np.asarray(desc.serialize(word_bw=w), dtype=np.uint64))
        else:
            y = self.filter_block(np.asarray(data), n, self.taps, self.carry, int(desc.zero_state))
            yield self.timeout(self._compute_delay(n))
            memw = MemWCmd(addr=int(desc.dst_off), len=nw, fwd_bursts=1)
            yield from self.cmd_out.write(np.asarray(memw.serialize(word_bw=w), dtype=np.uint64))
            yield from self.cmd_out.write(np.asarray(desc.serialize(word_bw=w), dtype=np.uint64))
            yield from self.cmd_out.write(np.asarray(y, dtype=np.uint64))

        self._log_firing()

    # --- the synthesizable bodies -------------------------------------------------------------

    @synthesizable
    def load_taps(self, x, n: int, taps: HwState) -> None:
        """Latch ``n`` coefficients into the held store.  The *load-once* flavour of state."""
        stored = unpack_samples(x, n, self.samp_cls, self.mem_dwidth)
        taps.val[:len(stored)] = stored

    @synthesizable
    def filter_block(self, x, n: int, taps: HwState, carry: HwState, zero_state: int):
        """One block through the filter: ``y[i] = sum_k h[k]·x[i-k]``, tail carried out.

        **This is the twin of both realizations.** ``unroll_lane`` changes how many samples the RTL
        consumes per iteration, not what it computes: same ``acc_t``, same rounding, same output. So
        there is one golden here and the parameter is a pure QoR knob.

        The accumulator format is **derived, not declared**: ``mult`` gives ``<2W, 2I>`` and
        ``fixed_sum`` over the ``T``-sample window adds ``ceil(log2 T)`` integer bits, so nothing here
        can silently overflow.  ``quantize`` back to the sample format is the one lossy step, and it is
        written down."""
        t = int(self.ntap)
        xs = unpack_samples(x, n, self.samp_cls, self.mem_dwidth)
        prev = np.zeros(t - 1, dtype=np.int64) if zero_state else np.asarray(carry.val, dtype=np.int64)

        # The window: buf[i : i+T] reversed is [x[i], x[i-1], ..., x[i-T+1]], aligned with h[0..T-1].
        buf = np.concatenate([prev, xs])
        win = np.lib.stride_tricks.sliding_window_view(buf, t)[:, ::-1]

        prod = mult(_as_fixed(win, self.samp_cls),
                    _as_fixed(np.asarray(taps.val, dtype=np.int64), self.samp_cls))
        acc = fixed_sum(prod, axis=1)                 # +ceil(log2 T) integer bits, NOT +T
        y = quantize(acc, self.samp_cls)

        carry.val[:] = buf[len(buf) - (t - 1):]       # the next block's initial condition
        return pack_samples(np.asarray(y).reshape(-1), self.samp_cls, self.mem_dwidth)

    def add_rm_self(self, platform):
        """A prior for the binding decisions, a fit for the estimated counters — one model, four
        counters, each from whichever of the two is honest for it.

        This is the only module in the design whose area moves with the knobs being explored, so it is
        the only one that needs a fit.  Its coefficients come from the committed corpus; in a design
        whose corpus lived in the platform library this would load from there instead, and the corpus
        is local here because it doubles as the example's test fixture.
        """
        from examples.fir_block.fir_block_corpus import points
        from examples.fir_block.fir_block_resource import fir_compute_fitted
        from waveflow.build.elaborate import elaborate

        samples = [(elaborate(FirCompute, {"mem_dwidth": 32, "ntap": n, "samp_w": w,
                                           "samp_i": 2, "unroll_lane": u}, name="fit"), m)
                   for n, w, u, m in points()]
        self._resource_model = fir_compute_fitted(platform=platform).fit(samples)


#: Seeded loop model for the sample loop — one output per cycle once the pipeline is full.  Replaced
#: by a measured fit (the interleaver's ``calibrate_compute.py`` shape) when the RTL exists.
FIR_COMPUTE_II_SEED = 1.0
FIR_COMPUTE_LATENCY_SEED = 8.0


# --- the composite ----------------------------------------------------------------------------


@dataclass
class FirBlock(FreeRunMod):
    """The block FIR composed on the framework in-band mem-streams: ``cmd_rx -> MemRStream ->
    fir_compute -> MemWStream``.  Every internal edge is framed; only the host boundary is plain.

    The read and write adaptors are framework components with shipped, XSI-verified timing, so this
    design owns exactly one custom thing — the stateful compute."""

    cpp_kernel_name: ClassVar[str | None] = "fir_block"

    mem_dwidth: HwParam[int] = MEM_DW
    ntap: HwParam[int] = DEFAULT_NTAP
    samp_w: HwParam[int] = DEFAULT_SAMP_W
    samp_i: HwParam[int] = DEFAULT_SAMP_I
    #: Threaded through to :class:`FirCompute` — the realization knob.  See its docstring.
    unroll_lane: HwParam[bool] = False
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    compute_calib_dir: "str | None" = None
    #: The calibration platform (the bus law + the mem-stream control residuals), passed to the
    #: framework mem-streams so each loads its shipped ``(component, platform)`` residual.
    platform_dir: "str | None" = None

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)

        self.rx = FirCmdRx(name=f"{self.name}_rx", sim=self.sim, mem_dwidth=w,
                           samp_w=int(self.samp_w), clk=self.clk)
        self.rstream = MemRStream(name=f"{self.name}_memr", sim=self.sim, mem_dwidth=w, inband=True,
                                  clk=self.clk, platform_dir=self.platform_dir)
        self.compute = FirCompute(name=f"{self.name}_compute", sim=self.sim, mem_dwidth=w,
                                  ntap=int(self.ntap), samp_w=int(self.samp_w),
                                  samp_i=int(self.samp_i), unroll_lane=bool(self.unroll_lane),
                                  clk=self.clk, calib_dir=self.compute_calib_dir)
        self.wstream = MemWStream(name=f"{self.name}_memw", sim=self.sim, mem_dwidth=w, inband=True,
                                  emit_done=True, clk=self.clk, platform_dir=self.platform_dir)
        for c in (self.rx, self.rstream, self.compute, self.wstream):
            self.add_comp(c)

        def _sif(name, master, slave):
            iface = StreamIF(name=f"{self.name}_{name}_if", sim=self.sim, clk=self.clk, bitwidth=w,
                             framed=True)
            iface.bind("master", master)
            iface.bind("slave", slave)
            self.add_if(iface)

        _sif("cmd_rd", self.rx.cmd_out, self.rstream.s_cmd)     # [MemRCmd | FirDesc]
        _sif("rdata", self.rstream.m_out, self.compute.s_in)    # [FirDesc | taps-or-block]
        _sif("wdata", self.compute.cmd_out, self.wstream.s_in)  # [MemWCmd | FirDesc | y]

        self.boundary = ["s_cmd", "m_in", "m_out", "s_done"]
        self.s_cmd = self.rx.s_cmd
        self.m_in = self.rstream.m_mem
        self.m_out = self.wstream.m_mem
        self.s_done = self.wstream.s_done

    def add_rm_self(self, platform):
        """The composite's OWN cost: `m_axi` adapters, channel FIFOs, control block, DATAFLOW shell.

        Keyed on boundary structure rather than on parameters, because that is what it depends on:
        measured invariant across all 24 compute configurations, and moving only when the memory word
        width did.  The table is built by elaborating one probe per measured width and asking each for
        its boundary signature, so the key is computed the same way the lookup will compute it.
        """
        from examples.fir_block.fir_block_corpus import INTERFACE_BY_MEM_DWIDTH
        from waveflow.build.elaborate import elaborate
        from waveflow.calib.resource_model import InterfaceResourceModel, boundary_signature

        table = {}
        for dw, counters in INTERFACE_BY_MEM_DWIDTH.items():
            probe = elaborate(FirBlock, {"mem_dwidth": dw, "ntap": 32, "samp_w": 16,
                                         "samp_i": 2, "unroll_lane": False}, name="probe")
            table[boundary_signature(probe)] = dict(counters)
        self._resource_model = InterfaceResourceModel(
            name="fir_block_interface", table=table, platform=platform)
