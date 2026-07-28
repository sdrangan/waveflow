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

**Transport is one sample per 32-bit word**, whatever ``samp_w`` is: the stored two's-complement value
sits in the low ``W`` bits.  That is a deliberate choice, not an oversight.  It keeps the width sweep
*clean* — only the arithmetic width moves, the bus and the word counts do not — and dense sub-word
packing is not expressible anyway at the interesting widths (the lane readers need an integer
``MEM_DW/elem_bw``, and ``W=18`` is not one).  Packing is the vectorization example's subject; see
``docs/guide/vectorization/hls/arrayutils.md``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataArray, DataList, EnumField, IntField
from waveflow.hw.fixpoint import FixedField, fixed_sum, mult, quantize
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam
from waveflow.hw.hw_state import HwState
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.mem_stream import KernelTask, MemRCmd, MemRStream, MemWCmd, MemWStream
from waveflow.hw.synth import synthesizable
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
SCHEMA_CLASSES = [FirCmd, FirDesc, MemRCmd, MemWCmd]
FRAMED_SCHEMAS = frozenset({FirDesc, MemRCmd, MemWCmd})


# --- format helpers ---------------------------------------------------------------------------


def samp_type(samp_w: int = DEFAULT_SAMP_W, samp_i: int = DEFAULT_SAMP_I) -> type[FixedField]:
    """The one sample/coefficient/output format — ``ap_fixed<W, I>``, signed.

    Built per-instance (the width is an :class:`HwParam`), which is why the hook's state arguments
    resolve their concrete type from the *registered instance* rather than from an annotation
    (``plans/add_state.md`` decision 5)."""
    return FixedField.specialize(W=int(samp_w), I=int(samp_i), signed=True)


def tap_array_type(ntap: int, samp_cls: type[FixedField]) -> type:
    """The coefficient store's schema.  ``cpp_storage="raw"`` is what lowers it to a bare
    ``Samp taps[T]`` — the form a ``static`` declaration wants."""
    return DataArray.specialize(samp_cls, max_shape=(int(ntap),), cpp_storage="raw")


def carry_array_type(ntap: int, samp_cls: type[FixedField]) -> type:
    """The per-block carry: the last ``T-1`` samples of the previous block."""
    return DataArray.specialize(samp_cls, max_shape=(max(int(ntap) - 1, 1),), cpp_storage="raw")


def words_to_stored(words, samp_w: int) -> np.ndarray:
    """Transport words -> stored two's-complement sample values (the low ``W`` bits, sign-extended)."""
    w = int(samp_w)
    v = np.asarray(words, dtype=np.uint64) & np.uint64((1 << w) - 1)
    neg = (v >> np.uint64(w - 1)) & np.uint64(1)
    return v.astype(np.int64) - (neg.astype(np.int64) << np.int64(w))


def stored_to_words(stored, samp_w: int) -> np.ndarray:
    """Stored sample values -> transport words (the inverse of :func:`words_to_stored`)."""
    w = int(samp_w)
    return (np.asarray(stored, dtype=np.int64) & ((1 << w) - 1)).astype(np.uint64)


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
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        self.s_cmd = StreamIFSlave(name=f"{self.name}_s_cmd", sim=self.sim, bitwidth=w,
                                   has_tlast=False)          # plain host boundary
        self.cmd_out = StreamIFMaster(name=f"{self.name}_cmd_out", sim=self.sim, bitwidth=w,
                                      has_tlast=True)        # framed -> MemRStream
        for ep in (self.s_cmd, self.cmd_out):
            self.add_endpoint(ep)
        self.fire_log: list[tuple[float, float]] = []

    def kernel_task(self) -> KernelTask:
        return KernelTask("fir_cmd_rx_task", "fir_cmd_rx_task.h", ("s_cmd", "cmd_out"),
                          template_args=(int(self.mem_dwidth),))

    def run_iter(self) -> ProcessGen[None]:
        w = int(self.mem_dwidth)
        cmd = yield from self.s_cmd.get(FirCmd)
        t0 = self.now
        desc = FirDesc(op=int(cmd.op), n=int(cmd.n), dst_off=int(cmd.dst_off),
                       zero_state=int(cmd.zero_state), tx_id=int(cmd.tx_id))
        memr = MemRCmd(addr=int(cmd.src_off), len=int(cmd.n), fwd_bursts=1)
        yield from self.cmd_out.write(np.asarray(memr.serialize(word_bw=w), dtype=np.uint64))
        yield from self.cmd_out.write(np.asarray(desc.serialize(word_bw=w), dtype=np.uint64))
        self.fire_log.append((t0 / self.clk.period, self.now / self.clk.period))


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
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    calib_dir: "str | None" = None

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        t = int(self.ntap)
        self.samp_cls = samp_type(self.samp_w, self.samp_i)

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

    def kernel_task(self) -> KernelTask:
        return KernelTask("fir_compute_task", "fir_compute_task.h", ("s_in", "cmd_out"),
                          template_args=(int(self.mem_dwidth), int(self.ntap)))

    def run_iter(self) -> ProcessGen[None]:
        w = int(self.mem_dwidth)
        desc = yield from self.s_in.get(FirDesc)
        n = int(desc.n)
        data = yield from self.s_in.get(nwords_max=n)
        t0 = self.now

        if int(desc.op) == FirOp.LOAD_TAPS:
            self.load_taps(np.asarray(data), self.taps)
            # No data written back -- but the job still completes.  len=0 makes the writer's S2A loop
            # trip zero times (no AXI transaction), while fwd_bursts=1 keeps the s_done echo.
            memw = MemWCmd(addr=int(desc.dst_off), len=0, fwd_bursts=1)
            yield from self.cmd_out.write(np.asarray(memw.serialize(word_bw=w), dtype=np.uint64))
            yield from self.cmd_out.write(np.asarray(desc.serialize(word_bw=w), dtype=np.uint64))
        else:
            y = self.filter_block(np.asarray(data), self.taps, self.carry, int(desc.zero_state))
            cycles = float(self.timing.predict({"n": n}))
            yield self.timeout(max(0.0, cycles) * self.clk.period)
            memw = MemWCmd(addr=int(desc.dst_off), len=n, fwd_bursts=1)
            yield from self.cmd_out.write(np.asarray(memw.serialize(word_bw=w), dtype=np.uint64))
            yield from self.cmd_out.write(np.asarray(desc.serialize(word_bw=w), dtype=np.uint64))
            yield from self.cmd_out.write(np.asarray(y, dtype=np.uint64))

        self.job_span_cyc.append((self.now - t0) / self.clk.period)
        self.fire_log.append((t0 / self.clk.period, self.now / self.clk.period))

    # --- the synthesizable bodies -------------------------------------------------------------

    @synthesizable
    def load_taps(self, x, taps: HwState) -> None:
        """Latch ``n`` coefficients into the held store.  The *load-once* flavour of state."""
        stored = words_to_stored(x, self.samp_w)
        taps.val[:len(stored)] = stored

    @synthesizable
    def filter_block(self, x, taps: HwState, carry: HwState, zero_state: int):
        """One block through the filter: ``y[i] = sum_k h[k]·x[i-k]``, tail carried out.

        The accumulator format is **derived, not declared**: ``mult`` gives ``<2W, 2I>`` and
        ``fixed_sum`` over the ``T``-sample window adds ``ceil(log2 T)`` integer bits, so nothing here
        can silently overflow.  ``quantize`` back to the sample format is the one lossy step, and it is
        written down."""
        t = int(self.ntap)
        xs = words_to_stored(x, self.samp_w)
        prev = np.zeros(t - 1, dtype=np.int64) if zero_state else np.asarray(carry.val, dtype=np.int64)

        # The window: buf[i : i+T] reversed is [x[i], x[i-1], ..., x[i-T+1]], aligned with h[0..T-1].
        buf = np.concatenate([prev, xs])
        win = np.lib.stride_tricks.sliding_window_view(buf, t)[:, ::-1]

        prod = mult(_as_fixed(win, self.samp_cls),
                    _as_fixed(np.asarray(taps.val, dtype=np.int64), self.samp_cls))
        acc = fixed_sum(prod, axis=1)                 # +ceil(log2 T) integer bits, NOT +T
        y = quantize(acc, self.samp_cls)

        carry.val[:] = buf[len(buf) - (t - 1):]       # the next block's initial condition
        return stored_to_words(np.asarray(y).reshape(-1), self.samp_w)


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
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    compute_calib_dir: "str | None" = None
    #: The calibration platform (the bus law + the mem-stream control residuals), passed to the
    #: framework mem-streams so each loads its shipped ``(component, platform)`` residual.
    platform_dir: "str | None" = None

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)

        self.rx = FirCmdRx(name=f"{self.name}_rx", sim=self.sim, mem_dwidth=w, clk=self.clk)
        self.rstream = MemRStream(name=f"{self.name}_memr", sim=self.sim, mem_dwidth=w, inband=True,
                                  clk=self.clk, platform_dir=self.platform_dir)
        self.compute = FirCompute(name=f"{self.name}_compute", sim=self.sim, mem_dwidth=w,
                                  ntap=int(self.ntap), samp_w=int(self.samp_w),
                                  samp_i=int(self.samp_i), clk=self.clk,
                                  calib_dir=self.compute_calib_dir)
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
