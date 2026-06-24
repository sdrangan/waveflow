"""fir.py — the matrix-LT FIR accelerator (block-fidelity ``HwComponent``).

This is the ``exec_model=hook`` + ``sim_fidelity=block`` version of the row-wise
FIR from ``plans/load_compute_store.md`` (the "Creating a matrix LT version"
section is authoritative).  It mirrors :mod:`examples.vmac.vmac`'s scaffolding —
an element-indexed memory region, the single ``@synthesizable`` hook, the bit-exact
golden, sim-only timing — but its **control is AXI-stream** (like ``shared_mem``: the
command rides ``s_in``, the response ``m_out``), not the AXI-MM command ring, and its timing
model is the **three persistent stage processes** (load / compute / store) over
**per-direction** bus resources, which is what the load-compute-store dataflow
needs and a single ``run_proc`` cannot express (it would serialize matrices and
idle the bus during compute).

Key modeling ideas (from the plan):

* **Three persistent processes.** ``load`` / ``compute`` / ``store`` each pull the
  next matrix as soon as they are free, so ``load(N+1)`` overlaps ``compute(N)``
  and ``store(N)`` — the throughput win of the structure.
* **Per-direction channel resources.** An ``m_axi`` bundle is full-duplex (Phase 1
  correction): the read (AR/R) and write (AW/W) channels are independent capacity-1
  resources, *not* one shared bus.  They are **owned by the interconnect/slave** (the
  contended memory port) and acquired automatically by the Region slice calls *through the
  interconnect* (no ``bus_rd``/``bus_wr`` wiring in the component).  ``compute`` runs from
  BRAM and holds neither, so it overlaps both; ``load(N+1)`` and ``store(N)`` use different
  channels and also overlap.  There is **no single-port II=2 floor** for a read+write kernel.
* **Fictitious inter-stage messages.** :class:`FIRCompMsg` / :class:`FIRStoreMsg`
  are plain dataclasses (never synthesized — in hardware the data moves through the
  partitioned-BRAM / FIFO channels); they carry the data plus an absolute-time
  ``tstart`` tag (the pipeline-fill quantity threaded through the stages).

**Timing uses the element-coordinate Region API end-to-end** (no raw bytes, no hand-rolled
retime).  The component supplies only the *structural* ``num_trans = n_row`` (one burst per
row) at the slice calls; the **interface** turns it into the deterministic channel-occupancy
span (``nwords + setup·num_trans`` — the :class:`~waveflow.hw.memif.BusTiming`).  The **store
is early-anchored** and finishes **under compute's shadow**:
``write_slice_pipelined(..., t_out_start=tstart_out, num_trans=n_row, min_span=compute_body)``
models the Y-write as beginning when the first Y row is ready and occupying the write channel
for ``max(write_occ, compute_body)``, so it *completes at* ``tstart_out + max(...)`` and
**overlaps the X-read** (the latency fix), hiding the channel under compute when compute is the
bottleneck.

**Codegen** is the static ``fir_top.cpp`` in :mod:`examples.rowwise_fir.fir_build`
(copied verbatim into ``gen/``), wrapping the ``@synthesizable(impl_file="fir_dataflow.tpp")``
hook — i.e. the validated Phase 1 sandbox ``fir_accel`` kernel.  The framework
``run_proc`` extractor is **not** used here: this ``run_proc`` is 3-process timing
orchestration (it never executes the hook), so it is not a valid extraction source.
Control is **AXI-stream**, like ``shared_mem`` — the host sends the command over
``s_in`` and reads the response on ``m_out`` — while the data stays on ``m_mem`` (the
synthesized top reads the command off the stream rather than s_axilite scalars).

**Timing is physical and near-fit-free** (``fir_calibrate.py``): channel occupancy is
deterministic (transfer beats == nwords), compute is II=1, and the only calibrated term is the
per-row pipeline refill depth ``row_depth(n_col)`` (a saturating
:class:`~waveflow.calib.InterpCalibModel` lookup).  The whole-kernel composes as
``(n_col+T) + trips + n_row·row_depth(n_col) + fill_const``.
:class:`FIRTiming` is loaded via :meth:`FIRTiming.from_calibration`
(``results/fir_calibration.json``); :meth:`FIRTiming.provisional` is a coarse seed.  Full
decomposition + gates: ``results/fir_calibration_results.md``.
"""
from __future__ import annotations

import enum
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList, FloatField, IntField, MemAddr
from waveflow.hw.hw_component import HwComponent, HwParam
from waveflow.calib import InterpCalibModel
from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
from waveflow.hw.memif import BusTiming, MMIFMaster
from waveflow.hw.synth import synthesizable
from waveflow.simulation.logger import NullLogger
from waveflow.simulation.simobj import ProcessGen

try:
    from examples.rowwise_fir.fir_golden import T, fir_golden
except ModuleNotFoundError:  # direct execution from the example dir
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from fir_golden import T, fir_golden  # type: ignore[no-redef]

# float32 memory element (1 word at mem_bw=32) — the FIR datatype.
Float32 = FloatField.specialize(bitwidth=32)
Word32 = IntField.specialize(bitwidth=32, signed=False)
Addr32 = MemAddr.specialize(bitwidth=32)


class FIROp(enum.IntEnum):
    """Command opcode: a FIR matrix, or the drain sentinel."""
    fir = 0
    end = 1


class FIRCmd(DataList):
    """One matrix-FIR command (host -> ``s_in`` stream -> accelerator).

    Addresses are **element (float) offsets** into the shared data region; the
    region base (``data_base``) is owned by the driver/system memory map.  ``h``
    lives in memory at ``h_off`` (read by the load stage), keeping the command a
    flat scalar record (so it serializes cleanly over the stream)."""

    elements = {
        "op":     {"schema": Word32, "description": "FIROp (fir / end)"},
        "tx_id":  {"schema": Word32, "description": "transaction id (timeline correlation)"},
        "x_off":  {"schema": Addr32, "description": "input matrix base (element offset)"},
        "h_off":  {"schema": Addr32, "description": "FIR taps base (element offset)"},
        "y_off":  {"schema": Addr32, "description": "output matrix base (element offset)"},
        "n_rows": {"schema": Word32, "description": "matrix rows"},
        "n_cols": {"schema": Word32, "description": "matrix cols"},
    }


class FIRResp(DataList):
    """One matrix-FIR response (accelerator -> ``m_out`` stream -> host).

    Emitted when a command completes (mirrors ``shared_mem``'s ``HistResp``); echoes
    the command's ``tx_id`` so the host can correlate, with a completion status."""

    elements = {
        "tx_id":  {"schema": Word32, "description": "echo of the command transaction id"},
        "status": {"schema": Word32, "description": "completion status (0 = ok)"},
    }


@dataclass
class FIRCompMsg:
    """load -> compute handoff (unsynthesized; data really moves via shared BRAM)."""
    tstart: float          # absolute sim-time the first row of X is available to compute
    X: np.ndarray          # loaded input matrix (n_rows, n_cols)
    h: np.ndarray          # FIR taps (T,)
    cmd: FIRCmd


@dataclass
class FIRStoreMsg:
    """compute -> store handoff (unsynthesized; data really moves via a FIFO)."""
    tstart: float          # absolute sim-time the first row of Y is available to store
    Y: np.ndarray          # computed output matrix (n_rows, out_len)
    cmd: FIRCmd
    compute_body: float = 0.0   # secs: the II=1 production span the store hides under (min_span)


@dataclass(frozen=True)
class LinSpan:
    """A fitted **linear primitive**: ``value = intercept + Σ coeff_i · row[name_i]``.

    The A4 pure-linear model — no ``sqrt`` fudge.  The bus spans use the physically
    grounded through-origin basis ``["num_trans", "nwords"]`` (``span = setup·num_trans +
    per_word·nwords``); the component ``fill`` uses ``["n_col"]`` with an intercept.  Has the
    ``predict(row) -> cycles`` shape :class:`~waveflow.hw.memif.BusTiming` consumes, is
    serialized as ``{"basis", "coeffs", "intercept"}`` in the calibration JSON, and is produced
    by :mod:`fir_calibrate` via :class:`waveflow.calib.LinCalibModel`."""

    basis: tuple[str, ...]
    coeffs: tuple[float, ...]
    intercept: float = 0.0

    def predict(self, row: dict) -> float:
        return self.intercept + sum(c * float(row[n]) for c, n in zip(self.coeffs, self.basis))

    @classmethod
    def from_dict(cls, d: dict) -> "LinSpan":
        return cls(basis=tuple(d["basis"]), coeffs=tuple(float(c) for c in d["coeffs"]),
                   intercept=float(d.get("intercept", 0.0)))


@dataclass(frozen=True)
class FIRTiming:
    """**Physical, near-fit-free** per-primitive timing model (cycles).  The whole-kernel
    decomposes — and the sim *composes* — as

    ::

        whole = (n_col + T) + trips + n_row·row_depth(n_col) + fill_const  (master equation)
              = fill + max(write_occ, compute_body)                        (the sim composition)

    with the pieces split by what they physically are (validated to ≤0.34% on the cosim grid):

    * **Channel occupancy — deterministic, fit-free.**  Each transfer beat is one word, so a
      burst occupies its channel for ``nwords + setup·num_trans`` cycles (``setup`` = the small
      fixed per-burst address latency).  This is the :class:`~waveflow.hw.memif.BusTiming` the
      slices consult; *not* a fitted curve.  (The old ``write_span`` wall-clock conflated this
      with the compute-stall — that conflation was the entire apparent "curvature".)
    * **Compute body — II=1, exact.**  ``compute_body = trips + (n_row−1)·row_depth(n_col)``: the
      steady MAC throughput is exactly one output/cycle (slope 1, *not fitted*); the only
      non-trivial part is the per-row pipeline refill ``row_depth``.
    * **``row_depth(n_col)`` — the ONE calibrated term.**  The per-row pipeline / 2-buffer
      ping-pong refill depth: a smooth, **saturating** 1-D curve (~70→260→268 cyc), carried as a
      calibrated :class:`~waveflow.calib.InterpCalibModel` *lookup* (not a ``sqrt`` fudge).  It
      appears ``n_row`` times — once in ``fill``, ``(n_row−1)`` times as inter-row gaps in the body.
      (It is dataflow refill depth, **not** compute latency — compute is exactly II=1.)
    * **``fill``** (ramp to first output) ``= (n_col+T) + row_depth(n_col) + fill_const``, and
      ``fill_const`` is a single calibrated constant (command→first-bus latency + drain edge).

    The store finishes **under compute's shadow**: its bus-visible span is
    ``max(write_occ, compute_body)`` (the ``min_span`` of
    :meth:`~waveflow.hw.memif.Region.write_slice_pipelined`), so when compute is the bottleneck
    the channel hides under it.  Load with :meth:`from_calibration`; :meth:`provisional` is a
    coarse seed."""

    row_depth: InterpCalibModel   # per-row pipeline refill depth row_depth(n_col) — the ONE calibrated term
    fill_const: float         # ramp/drain constant (command->first-bus latency + drain edge)
    setup: float = 2.0        # per-burst address latency (deterministic channel occupancy)
    resp_words: float = 4.0   # small response write after Y (cycles ~= resp_words)
    write_per_word: float = 1.0

    def row_depth_cyc(self, n_cols: int) -> float:
        return max(0.0, self.row_depth.predict({"n_col": float(n_cols)}))

    def bus_timing(self, clk_freq: float) -> BusTiming:
        """Deterministic per-direction channel occupancy ``nwords + setup·num_trans`` (slope 1,
        fit-free).  Both directions share the same form; the interface supplies the direction's
        ``nwords`` and ``num_trans`` at the slice call."""
        occ = LinSpan(("num_trans", "nwords"), (self.setup, 1.0))
        return BusTiming(read=occ, write=occ, clk_freq=clk_freq)

    def t_fill_cyc(self, n_rows: int, n_cols: int) -> float:
        """Ramp to first output (Y-write start offset): ``(n_col+T) + row_depth(n_col) + fill_const``."""
        return max(0.0, (n_cols + T) + self.row_depth_cyc(n_cols) + self.fill_const)

    def compute_body_cyc(self, n_rows: int, n_cols: int) -> float:
        """II=1 production span of all outputs: ``trips + (n_row−1)·row_depth(n_col)``."""
        trips = n_rows * (n_cols - T + 1)
        return max(0.0, trips + (n_rows - 1) * self.row_depth_cyc(n_cols))

    @classmethod
    def provisional(cls) -> "FIRTiming":
        """Coarse seed (flat row_depth ≈ pipeline depth) used before calibration runs."""
        row_depth = InterpCalibModel.from_samples("n_col", [64.0, 1024.0], [70.0, 268.0], "row_depth")
        return cls(row_depth=row_depth, fill_const=59.0)

    @classmethod
    def from_calibration(cls, path) -> "FIRTiming":
        """Load the calibrated ``row_depth`` table + constants from a fir_calibrate.py JSON artifact."""
        d = json.loads(Path(path).read_text())["models"]
        rd = d["row_depth"]
        row_depth = InterpCalibModel.from_samples(rd["feature"], rd["x"], rd["y"], "row_depth")
        return cls(row_depth=row_depth, fill_const=float(d["fill_const"]), setup=float(d.get("setup", 2.0)))


@dataclass
class FIRAccel(HwComponent):
    """Matrix-LT FIR accelerator: AXI-stream control (``s_in``/``m_out``, like ``shared_mem``)
    + the 3-process block timing model over per-direction bus resources, wrapping the
    ``fir_dataflow`` hook."""

    cpp_kernel_name: ClassVar[str | None] = "fir"
    cpp_namespace: ClassVar[str | None] = "fir_dataflow"

    mem_dwidth: HwParam[int] = 32       # m_axi data width (float32 -> 1 word/element)
    mem_awidth: HwParam[int] = 32
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))  # 10 ns/cycle (matches cosim)

    def __post_init__(self) -> None:
        super().__post_init__()
        # Control rides AXI-stream (command on s_in, response on m_out, like shared_mem);
        # the data stays on the m_mem AXI-MM bundle.
        self.s_in = StreamIFSlave(
            name=f"{self.name}_s_in", sim=self.sim, bitwidth=int(self.mem_dwidth))
        self.m_out = StreamIFMaster(
            name=f"{self.name}_m_out", sim=self.sim, bitwidth=int(self.mem_dwidth))
        self.m_mem = MMIFMaster(
            name=f"{self.name}_m_mem", sim=self.sim, bitwidth=int(self.mem_dwidth)
        )
        for ep in (self.s_in, self.m_out, self.m_mem):
            self.add_endpoint(ep)
        # Attached by the driver (owns the system memory map) before run_sim:
        self.data_base: int = 0          # byte base of the shared data region
        self.timing = FIRTiming.provisional()   # driver overrides with the fitted calibration
        self.logger = NullLogger()
        # timeline capture (per-command stage events), correlated by tx_id
        self.events: list[dict] = []
        self._mem_bw = int(self.mem_dwidth)

    @property
    def Cmd(self) -> type[FIRCmd]:
        return FIRCmd

    # --- the golden ----------------------------------------------------------
    def execute(self, X: np.ndarray, h: np.ndarray) -> np.ndarray:
        """The pure bit-exact golden — the ONE shared ``fir_golden`` (no memory/timing)."""
        return fir_golden(X, h)

    # --- the (sim-only) timeline logger -------------------------------------
    def _log(self, event: str, cmd: FIRCmd, *, t: float | None = None, **extra) -> None:
        """Record a timeline event.  *t* overrides the timestamp — used for the
        early-anchored ``store_begin`` (the Y-write *effectively* begins at the early
        anchor ``tstart_out``, before the late ``bus_wr`` acquire), so the logged
        bus-visible span matches RTL's overlapped Y-write burst."""
        tt = float(self.now) if t is None else float(t)
        rec = {"event": event, "tx_id": int(cmd.tx_id), "t": tt,
               "n_rows": int(cmd.n_rows), "n_cols": int(cmd.n_cols), **extra}
        self.events.append(rec)
        self.logger.log(role="fir", event=event, tx_id=int(cmd.tx_id),
                        n_rows=int(cmd.n_rows), n_cols=int(cmd.n_cols),
                        tstart=tt, **extra)

    # --- the synthesizable hook (codegen only; never executed in sim) ---------
    @synthesizable(impl_file="fir_dataflow.tpp")
    def dataflow(self, cmd: FIRCmd, mem) -> ProcessGen[None]:
        """The single synthesizable unit: the whole load-compute-store DATAFLOW kernel.

        **Body is a bare ``yield`` (a stub).**  Codegen does not introspect this body: the
        ``@synthesizable`` decorator records only the signature ``(self, cmd, mem)`` (mapped to
        the C++ call args) and ``impl_file="fir_dataflow.tpp"``; the m_axi top is the static
        ``fir_top.cpp`` (copied into ``gen/`` by :mod:`fir_build`), which calls the hand-written
        ``fir_dataflow.tpp`` core (= the validated Phase 1 sandbox ``fir_accel``).  And **in sim
        this is never run** — the
        three stage processes below model the timing.  So the body has no source-of-truth role;
        it stays a generator (the ``yield``) only so ``@synthesizable`` sees a coroutine.

        Element-coordinate contract (what ``fir_dataflow.tpp`` implements, for the record):
        read ``X = mem[x_off : x_off + n_rows*n_cols]`` and ``h = mem[h_off : h_off + T]``,
        compute ``Y = fir_golden(X.reshape(n_rows, n_cols), h)``, and write
        ``mem[y_off : ...] = Y.ravel()`` — all in element (float) coordinates; the Region owns
        the element->byte mapping."""
        yield

    # --- helpers -------------------------------------------------------------
    def _secs(self, cycles: float) -> float:
        return cycles / float(self.clk.freq)

    # --- lifecycle -----------------------------------------------------------
    def pre_sim(self) -> None:
        super().pre_sim()
        # Sanctioned SimPy primitives (waveflow/simulation/simobj.py).
        self.load_q = self.transaction_queue()      # run_proc -> load
        self.compute_q = self.transaction_queue()   # load -> compute
        self.store_q = self.transaction_queue()     # compute -> store
        # The full-duplex AR/R and AW/W channels + the per-direction X-read / Y-write SPANS
        # (``bus_timing``) live on the **interconnect/slave** (the contended memory port), not
        # on this accelerator: the Region slice calls reach them through the interconnect, which
        # decodes the address to the serving slave.  The component supplies only the *access
        # pattern* — ``num_trans = n_row`` (one burst per row) — at the slice call; the platform
        # wiring configures the slave's ``bus_timing`` (see fir_sim.py).
        self._data = self.m_mem.region(self.data_base, Float32, word_bw=self._mem_bw)
        # Write-channel effective-free time: successive Y-writes serialize by their EARLY-
        # ANCHORED effective spans (not just the late resource acquire), so back-to-back
        # throughput is correct (resolves the write-channel "acquired late" caveat).
        self._bus_wr_free = 0.0

    def pre_sim_processes(self):  # documented spawn point — see pre_sim
        pass

    def run_proc(self) -> ProcessGen[None]:
        """Host-facing entry: pull commands off the ``s_in`` stream and kick the pipeline.

        Spawns the three persistent stage processes on first entry, then forwards each
        command to the load stage without blocking on completion (the pipeline overlaps).
        Each command's response is emitted on ``m_out`` by the store stage when it
        completes (like ``shared_mem``)."""
        self.process(self.load())
        self.process(self.compute())
        self.process(self.store())
        while True:
            cmd: FIRCmd = yield from self.s_in.get(self.Cmd)
            if int(cmd.op) == int(FIROp.end):
                self.load_q.put(cmd)   # forward the sentinel so the stages drain + stop
                return
            self.load_q.put(cmd)

    def load(self) -> ProcessGen[None]:
        while True:
            cmd: FIRCmd = yield self.load_q.get()
            self._log("cmd_arrive", cmd)
            if int(cmd.op) == int(FIROp.end):
                empty = np.zeros((0, 0), dtype=np.float32)
                self.compute_q.put(FIRCompMsg(tstart=self.now, X=empty, h=empty, cmd=cmd))
                return
            n_rows, n_cols = int(cmd.n_rows), int(cmd.n_cols)
            t_begin = self.now
            self._log("load_begin", cmd)            # == start of the X-read burst in RTL
            # Element-coordinate read: h (small, subsumed) then the X-read.  The read channel
            # is acquired automatically by the Region, and the X-read SPAN is the interface's
            # calibrated bus_timing for ``num_trans = n_row`` bursts — anchored at the load
            # start.  No byte arithmetic, no hand-rolled duration, no bus_rd wiring.
            #
            # DEFERRED (plan item 4): re-anchoring at the *actual* X-read start (drop
            # ``t_out_start`` so the returned ``x_tstart`` is used downstream) would be the clean
            # attribution for read-bound kernels, with the coupled ``+T`` taken OUT of ``fill``.
            # It is masked for FIR (the write channel anchors throughput) and is NOT a free pure
            # refactor here: the h read also incurs the crossbar's address/return latency, so
            # ``x_tstart - load_start`` is ``T + 2·latency`` cycles, not ``T`` — cancelling it
            # would force re-deriving ``fill_const`` and shift the gates.  Left as-is; revisit
            # when the interconnect ("B") owns bus timing.
            hf = yield from self._data.read_slice(int(cmd.h_off), int(cmd.h_off) + T)
            Xf, _ = yield from self._data.read_slice_pipelined(
                int(cmd.x_off), int(cmd.x_off) + n_rows * n_cols,
                t_out_start=t_begin, num_trans=n_rows)
            self._log("load_end", cmd)              # == end of the X-read burst
            # tstart = load start; the first-Y-row fill (t_fill) is added in compute as the
            # Y-write anchor.  (compute is hidden, so the input-row split is immaterial.)
            tstart = t_begin
            X = np.asarray(Xf, dtype=np.float32).reshape(n_rows, n_cols)
            self.compute_q.put(FIRCompMsg(tstart=tstart, X=X, h=np.asarray(hf, dtype=np.float32),
                                          cmd=cmd))

    def compute(self) -> ProcessGen[None]:
        while True:
            msg: FIRCompMsg = yield self.compute_q.get()
            if int(msg.cmd.op) == int(FIROp.end):
                self.store_q.put(FIRStoreMsg(tstart=self.now,
                                             Y=np.zeros((0, 0), dtype=np.float32), cmd=msg.cmd))
                return
            n_rows, n_cols = int(msg.cmd.n_rows), int(msg.cmd.n_cols)
            Y = self.execute(msg.X, msg.h)          # the ONE shared golden
            # first Y row available to store = load start + the ramp-to-first-output fill;
            # compute_body = the II=1 production span the store then hides under (the
            # compute-shadow floor passed to the Y-write).  Compute holds no bus channel, so it
            # overlaps load(N+1)/store(N) — the timing is carried by the store's anchored span.
            tstart_out = msg.tstart + self._secs(self.timing.t_fill_cyc(n_rows, n_cols))
            compute_body = self._secs(self.timing.compute_body_cyc(n_rows, n_cols))
            self._log("comp_begin", msg.cmd, compute_body=float(compute_body))
            self.store_q.put(FIRStoreMsg(tstart=tstart_out, Y=Y, cmd=msg.cmd,
                                         compute_body=compute_body))

    def store(self) -> ProcessGen[None]:
        while True:
            msg: FIRStoreMsg = yield self.store_q.get()
            if int(msg.cmd.op) == int(FIROp.end):
                return                      # drain complete
            n_rows = int(msg.cmd.n_rows)
            # EARLY-ANCHORED Y-write in ONE element-coordinate call: the Region writes Y
            # functionally, acquires the write channel automatically, and occupies it for
            # max(channel occupancy, compute_body) — the store finishes UNDER COMPUTE'S SHADOW
            # (min_span=compute_body) — anchored at tstart_out (when the first Y row was ready),
            # so it completes at tstart_out + max(...), OVERLAPPING the X-read (the latency fix)
            # and hiding the channel under compute when compute is the bottleneck.
            #
            # Serialize successive writes by their EFFECTIVE spans: the Y-write cannot begin
            # before the previous Y-write's effective end (the write channel is busy), even
            # though its first row was ready earlier.  This is what makes back-to-back
            # THROUGHPUT correct (single-matrix latency is unchanged: _bus_wr_free starts at 0).
            anchor = max(msg.tstart, self._bus_wr_free)
            t0, t1 = yield from self._data.write_slice_pipelined(
                int(msg.cmd.y_off), msg.Y.ravel(), t_out_start=anchor,
                num_trans=n_rows, min_span=msg.compute_body, element_type=Float32)
            self._bus_wr_free = t1
            self._log("store_begin", msg.cmd, t=t0)   # effective Y-write start
            self._log("store_end", msg.cmd, t=t1)     # == end of the Y-write burst
            # response back to the host on the m_out stream (like shared_mem's respond) —
            # the small post-Y latency is preserved, then the FIRResp is emitted.
            yield self.timeout(self._secs(self.timing.resp_words * self.timing.write_per_word))
            self._log("resp_sent", msg.cmd)
            yield from self.m_out.write(FIRResp(tx_id=int(msg.cmd.tx_id), status=0))
