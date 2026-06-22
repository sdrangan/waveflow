"""fir.py — the matrix-LT FIR accelerator (block-fidelity ``HwComponent``).

This is the ``exec_model=hook`` + ``sim_fidelity=block`` version of the row-wise
FIR from ``plans/load_compute_store.md`` (the "Creating a matrix LT version"
section is authoritative).  It mirrors :mod:`examples.vmac.vmac`'s scaffolding —
an AXI-MM command ring, an element-indexed memory region, the single
``@synthesizable`` hook, the bit-exact golden, sim-only timing — but its timing
model is the **three persistent stage processes** (load / compute / store) over
**per-direction** bus resources, which is what the load-compute-store dataflow
needs and a single ``run_proc`` cannot express (it would serialize matrices and
idle the bus during compute).

Key modeling ideas (from the plan):

* **Three persistent processes.** ``load`` / ``compute`` / ``store`` each pull the
  next matrix as soon as they are free, so ``load(N+1)`` overlaps ``compute(N)``
  and ``store(N)`` — the throughput win of the structure.
* **Per-direction channel resources.** An ``m_axi`` bundle is full-duplex (Phase 1
  correction): ``bus_rd`` (AR/R) and ``bus_wr`` (AW/W) are independent capacity-1
  resources, *not* one shared bus.  ``compute`` runs from BRAM and holds neither,
  so it overlaps both; ``load(N+1)`` and ``store(N)`` use different channels and
  also overlap.  There is **no single-port II=2 floor** for a read+write kernel.
* **Fictitious inter-stage messages.** :class:`FIRCompMsg` / :class:`FIRStoreMsg`
  are plain dataclasses (never synthesized — in hardware the data moves through the
  partitioned-BRAM / FIFO channels); they carry the data plus an absolute-time
  ``tstart`` tag (the pipeline-fill quantity threaded through the stages).

**Timing uses the element-coordinate Region API end-to-end** (no raw bytes, no hand-rolled
retime).  The read/write channel SPANS are ``FIRTiming.t_load/t_store``
(``setup*n_row + per_word*words``) — **bilinear** (Phase 1), which the linear ``word_bw``
bus model cannot represent — so they are passed as ``duration=`` to the Region's pipelined
slice calls, which do the functional movement AND occupy the channel for exactly that span
(the movement's own word_bw bus time is subsumed; no double-count).  The **store is
early-anchored**: ``write_slice_pipelined(..., t_out_start=tstart_out, duration=t_store)``
models the Y-write as beginning when the first Y row is ready, so it *completes at*
``tstart_out + t_store`` and **overlaps the X-read** (the latency fix).  ``bus_rd`` /
``bus_wr`` are held around the calls to serialize successive same-direction transfers
(the inter-matrix throughput model).

**Codegen** is the hand-rolled ``render_top`` in :mod:`examples.rowwise_fir.fir_build`
(VMAC's *primary* pattern), wrapping the ``@synthesizable(impl_file="fir_dataflow.tpp")``
hook — i.e. the validated Phase 1 sandbox ``fir_accel`` kernel.  The framework
``run_proc`` extractor is **not** used here: this ``run_proc`` is 3-process timing
orchestration (it never executes the hook), so it is not a valid extraction source.
The AXIMMQueue ring is sim-only; the synthesized kernel takes the command as
s_axilite scalars (exactly as VMAC bakes its command).

**Timing parameters are PROVISIONAL** (seeded from the Phase 1 bilinear fit). The
per-stage cosim calibration (read-channel / write-channel / compute split, ≥3
``n_row``, the back-to-back 2-matrix point, the sklearn fit) is the deferred
follow-step; :class:`FIRTiming` is where the real numbers plug in.
"""
from __future__ import annotations

import enum
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList, FloatField, IntField, MemAddr
from waveflow.hw.hw_component import HwComponent, HwParam
from waveflow.hw.memif import MMIFMaster
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
    """One matrix-FIR command (host -> ring -> accelerator).

    Addresses are **element (float) offsets** into the shared data region; the
    region base (``data_base``) is owned by the driver/system memory map.  ``h``
    lives in memory at ``h_off`` (read by the load stage), keeping the command a
    flat scalar record (so it serializes cleanly through the ring)."""

    elements = {
        "op":     {"schema": Word32, "description": "FIROp (fir / end)"},
        "tx_id":  {"schema": Word32, "description": "transaction id (timeline correlation)"},
        "x_off":  {"schema": Addr32, "description": "input matrix base (element offset)"},
        "h_off":  {"schema": Addr32, "description": "FIR taps base (element offset)"},
        "y_off":  {"schema": Addr32, "description": "output matrix base (element offset)"},
        "n_rows": {"schema": Word32, "description": "matrix rows"},
        "n_cols": {"schema": Word32, "description": "matrix cols"},
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


@dataclass(frozen=True)
class FIRTiming:
    """PROVISIONAL per-stage timing budget (cycles), seeded from the Phase 1 bilinear fit.

    The matrix-LT model needs the budget **split by the resource it consumes** so the
    read-channel, write-channel, and compute timelines overlap correctly:

    * ``read_setup`` + ``read_per_word`` -> ``bus_rd`` hold (the X-read burst span).
    * ``write_setup`` + ``write_per_word`` -> ``bus_wr`` hold (the Y-write burst span).
    * ``comp_fill`` + ``comp_per_col``/``comp_per_row`` -> the BRAM compute time that
      overlaps both channels (the ``L0 + L_col*n_col`` fill of the Phase 1 fit; compute
      is II=1 so the per-output rate is hidden under memory for normal T).

    These numbers are PROVISIONAL — seeded to the single 4x64 cosim point (X-read 392,
    Y-write 437, whole-kernel 656 cyc) so the overlap can be checked.  >>> The real
    per-stage cosim calibration (X-read span / Y-write span / load_end->store_begin gap,
    >=3 n_row, sklearn bilinear fit) replaces every field here. <<<  See
    plans/load_compute_store.md "Calibration".

    Spans are PER-ROW-AWARE (``setup*n_row + per_word*words``) so they can match RTL's
    bilinear shape — the linear bus model cannot, which is why FIR drives the spans from
    these timeouts rather than the Region's bus-timed transfer (see the class docstring).
    """
    # read channel (per row: setup + a burst of n_col input words); 4*(34+64)=392 @ 4x64
    read_setup: float = 34.0
    read_per_word: float = 1.0
    # write channel (per row: setup + a burst of (n_col-T+1) output words); 4*(52.25+57)=437 @ 4x64
    write_setup: float = 52.25
    write_per_word: float = 1.0
    # compute (BRAM; hidden under memory for normal T -> keep t_compute small so t_tail=0)
    comp_fill: float = 30.0
    comp_per_col: float = 0.0
    comp_per_row: float = 0.0
    comp_row_fill: float = 121.0    # first-Y-row latency after the first input row lands
    # response write (small burst on bus_wr after Y)
    resp_words: float = 4.0

    def t_row_load_cyc(self, n_cols: int) -> float:
        return self.read_setup + self.read_per_word * n_cols

    def t_load_cyc(self, n_rows: int, n_cols: int) -> float:
        return n_rows * self.t_row_load_cyc(n_cols)

    def t_row_store_cyc(self, n_cols: int) -> float:
        return self.write_setup + self.write_per_word * (n_cols - T + 1)

    def t_store_cyc(self, n_rows: int, n_cols: int) -> float:
        return n_rows * self.t_row_store_cyc(n_cols)

    def t_compute_cyc(self, n_rows: int, n_cols: int) -> float:
        return self.comp_fill + self.comp_per_col * n_cols + self.comp_per_row * n_rows

    def t_row_compute_cyc(self, n_cols: int) -> float:
        return self.comp_row_fill


@dataclass
class FIRAccel(HwComponent):
    """Matrix-LT FIR accelerator: AXI-MM command ring + the 3-process block timing model
    over per-direction bus resources, wrapping the ``fir_dataflow`` hook."""

    cpp_kernel_name: ClassVar[str | None] = "fir"
    cpp_namespace: ClassVar[str | None] = "fir_dataflow"

    mem_dwidth: HwParam[int] = 32       # m_axi data width (float32 -> 1 word/element)
    mem_awidth: HwParam[int] = 32
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))  # 10 ns/cycle (matches cosim)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.m_mem = MMIFMaster(
            name=f"{self.name}_m_mem", sim=self.sim, bitwidth=int(self.mem_dwidth)
        )
        self.add_endpoint(self.m_mem)
        # Attached by the driver (owns the system memory map) before run_sim:
        self.cmd_queue = None            # AXIMMQueue (sim-only command ring)
        self.data_base: int = 0          # byte base of the shared data region
        self.timing = FIRTiming()        # PROVISIONAL; replaced by the deferred calibration
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

        Codegen emits the m_axi top (``fir_build.render_top``) that calls the hand-written
        ``fir_dataflow.tpp`` core (= the validated Phase 1 sandbox ``fir_accel``).  **In sim
        this is never run** — the three stage processes below model its timing; its body is
        documentary so the golden/codegen contract is explicit (element-coordinate access; the
        Region owns element->byte)."""
        n, m = int(cmd.n_rows), int(cmd.n_cols)
        region = self.m_mem.region(self.data_base, Float32, word_bw=self._mem_bw)
        X = yield from region.read_slice(int(cmd.x_off), int(cmd.x_off) + n * m)
        h = yield from region.read_slice(int(cmd.h_off), int(cmd.h_off) + T)
        Y = self.execute(np.asarray(X).reshape(n, m), np.asarray(h))
        yield from region.write_slice(int(cmd.y_off), Y.ravel(), element_type=Float32)

    # --- helpers -------------------------------------------------------------
    def _secs(self, cycles: float) -> float:
        return cycles / float(self.clk.freq)

    # --- lifecycle -----------------------------------------------------------
    def pre_sim(self) -> None:
        super().pre_sim()
        if self.cmd_queue is None:
            raise RuntimeError("FIRAccel requires an attached cmd_queue (set by the driver).")
        # Sanctioned SimPy primitives (waveflow/simulation/simobj.py).
        self.load_q = self.transaction_queue()      # run_proc -> load
        self.compute_q = self.transaction_queue()   # load -> compute
        self.store_q = self.transaction_queue()     # compute -> store
        # Per-DIRECTION channel resources (full-duplex bundle): independent, capacity-1.
        self.bus_rd = self.resource(capacity=1)     # AR/R — successive loads serialize here
        self.bus_wr = self.resource(capacity=1)     # AW/W — successive stores serialize here
        # Element-coordinate view of the shared data region (the Region owns element->byte).
        # Data moves through it; the per-stage SPANS are passed explicitly as `duration=` to the
        # pipelined slice calls (per-row-aware / bilinear — the linear word_bw model can't express
        # them), and the functional movement's own bus time is subsumed into that duration (no
        # double-count).  Poll the ring on a coarse cadence (cmd_arrive offset anchored away).
        self._data = self.m_mem.region(self.data_base, Float32, word_bw=self._mem_bw)
        self.cmd_queue.poll_interval = 64.0

    def pre_sim_processes(self):  # documented spawn point — see pre_sim
        pass

    def run_proc(self) -> ProcessGen[None]:
        """Host-facing entry: pull commands off the ring and kick the pipeline.

        Spawns the three persistent stage processes on first entry, then forwards each
        command to the load stage without blocking on completion (the pipeline overlaps)."""
        self.process(self.load())
        self.process(self.compute())
        self.process(self.store())
        while True:
            cmd: FIRCmd = yield from self.cmd_queue.get(self.Cmd)
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
            with self.bus_rd.request() as req:      # read channel (serializes successive loads)
                yield req
                t_begin = self.now
                self._log("load_begin", cmd)        # == start of the X-read burst in RTL
                # Element-coordinate read: h (small, subsumed) then the X-read whose SPAN is the
                # per-row-aware t_load, anchored at the load start.  The Region does the
                # functional movement and occupies bus_rd for exactly the duration (no
                # double-count, no byte arithmetic, no hand-rolled timeout).
                hf = yield from self._data.read_slice(int(cmd.h_off), int(cmd.h_off) + T)
                Xf, _ = yield from self._data.read_slice_pipelined(
                    int(cmd.x_off), int(cmd.x_off) + n_rows * n_cols,
                    t_out_start=t_begin, duration=self._secs(self.timing.t_load_cyc(n_rows, n_cols)))
                self._log("load_end", cmd)          # == end of the X-read burst
            # tstart = ABSOLUTE time the first input row is available to compute (pipeline-fill
            # anchor): load start + one row's read-channel time.
            tstart = t_begin + self._secs(self.timing.t_row_load_cyc(n_cols))
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
            # absolute completion = first-input-row time + whole-matrix compute time.
            t_compute = self._secs(self.timing.t_compute_cyc(n_rows, n_cols))
            t_done = msg.tstart + t_compute
            t_tail = max(t_done - self.now, 0.0)    # remainder after the load tail (overlap credit)
            self._log("comp_begin", msg.cmd, t_compute=float(t_compute), t_tail=float(t_tail))
            Y = self.execute(msg.X, msg.h)          # the ONE shared golden
            yield self.timeout(t_tail)
            # first Y row available to store = first input row + one row's compute time.
            tstart_out = msg.tstart + self._secs(self.timing.t_row_compute_cyc(n_cols))
            self.store_q.put(FIRStoreMsg(tstart=tstart_out, Y=Y, cmd=msg.cmd))

    def store(self) -> ProcessGen[None]:
        while True:
            msg: FIRStoreMsg = yield self.store_q.get()
            if int(msg.cmd.op) == int(FIROp.end):
                return                      # drain complete
            n_rows, n_cols = int(msg.cmd.n_rows), int(msg.cmd.n_cols)
            with self.bus_wr.request() as req:      # write channel (independent of bus_rd)
                yield req
                # EARLY-ANCHORED, per-row-aware Y-write in ONE element-coordinate call: the
                # Region writes Y functionally AND occupies bus_wr for exactly t_store, anchored
                # at tstart_out (when the first Y row was ready) — so it completes at
                # tstart_out + t_store, OVERLAPPING the X-read instead of following it (the
                # latency fix).  No byte arithmetic, no hand-rolled anchor/timeout.
                t_store = self._secs(self.timing.t_store_cyc(n_rows, n_cols))
                t0, t1 = yield from self._data.write_slice_pipelined(
                    int(msg.cmd.y_off), msg.Y.ravel(), t_out_start=msg.tstart,
                    duration=t_store, element_type=Float32)
                self._log("store_begin", msg.cmd, t=t0)   # effective Y-write start
                self._log("store_end", msg.cmd, t=t1)     # == end of the Y-write burst
                # response back to the host — a small write burst on bus_wr after the Y writes.
                yield self.timeout(self._secs(self.timing.resp_words * self.timing.write_per_word))
                self._log("resp_sent", msg.cmd)
