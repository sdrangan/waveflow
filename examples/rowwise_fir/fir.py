"""fir.py — free-running streaming FIR accelerator (Waveflow integration, Stage B).

The Waveflow integration of the validated free-running streaming FIR sandbox
(``sandbox/fir_freerun_sandbox.cpp``; ``plans/fir_freerun_integration.md``).  Supersedes the
control-driven Phase-2 and the matrix-LT block kernel.

**Exec model = FREE-RUNNING.**  ``FIRAccel`` declares only ``s_in`` / ``m_out`` / ``m_mem`` (no
``VitisRegMapMMIFSlave``), so :func:`~waveflow.build.hwcodegen.extract_kernel` picks ``run_proc``
and :func:`~waveflow.build.hwgen.kernel_signature` emits an **``ap_ctrl_hs``** top.  Errors ride
the **response stream** (per-job ``status`` on ``m_out``), not a status regmap.

``run_proc`` is the codegen marker: a single call to the ``@synthesizable`` :meth:`pipeline`
hook.  Codegen replaces the hook body with ``fir_pipeline_impl.tpp`` (the DATAFLOW
load/compute/store + shift-register FIR + END-drain + per-job status, ``ap_uint<MEM_DW>*`` gmem
via ``read_array_slice``/``write_array_slice``).  **In sim the hook body IS the model.**

**Stage-B sim = the streaming pipeline.**  The hook's sim body spawns **three persistent stage
processes** (``load`` / ``compute`` / ``store``) wired by unbounded job queues — the sim twin of
the kernel's three ``#pragma HLS DATAFLOW`` ``while(!done)`` stages.  ``run_proc`` pulls each
command off ``s_in`` and kicks ``load`` **without blocking**, so successive jobs overlap exactly
as in the kernel (``load(N+1) ∥ store(N)``).  The per-stage durations come from :class:`FIRTiming`
(calibrated to the freerun cosim by ``fir_calibrate.py``): ``store`` carries the steady throughput
**period** (the bottleneck), ``load`` + ``compute`` carry the pipeline **fill**, so a batch of
``n`` jobs takes ``fill + n*period`` — matching the cosim (``704`` cyc/job @ 4×64, fill ≈ ``396``).
The functional truth (``execute``) is bit-exact-shared with the kernel; only the timing is modeled.
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
from waveflow.hw.dataschema import DataList, EnumField, FloatField, IntField, MemAddr
from waveflow.hw.hw_component import HwComponent, HwParam
from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
from waveflow.hw.memif import MMIFMaster
from waveflow.hw.synth import synthesizable
from waveflow.simulation.logger import NullLogger
from waveflow.simulation.simobj import ProcessGen

try:
    from examples.rowwise_fir.fir_golden import T, fir_golden
except ModuleNotFoundError:  # direct execution from the example dir
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from fir_golden import T, fir_golden  # type: ignore[no-redef]

# float32 memory element (1 word at mem_bw=32) — the FIR datatype.  ``include_dir`` puts the
# generated ``float32_array_utils.h`` under ``include/`` (resolved by the ``-I.`` cflag), like poly.
Float32 = FloatField.specialize(bitwidth=32, include_dir="include")
Word32 = IntField.specialize(bitwidth=32, signed=False)
Word16 = IntField.specialize(bitwidth=16, signed=False)
Addr32 = MemAddr.specialize(bitwidth=32)

# Valid-size bounds (mirror the sandbox); a command outside these is a per-job error.
NCOL_MAX = 1024
MAX_ROWS = 64

WORD_BW_SUPPORTED = [32, 64]


class FIROp(enum.IntEnum):
    """Command opcode (on ``s_in``): a FIR matrix, or the drain sentinel."""
    fir = 0
    end = 1


class FIRStatus(enum.IntEnum):
    """Per-job control/status carried on the internal ctrl stream and (ok/bad_size) in the
    response.  ``end`` is the ctrl-only drain marker the END sentinel rides through the pipeline."""
    ok = 0
    bad_size = 1
    end = 2


FIROpField = EnumField.specialize(enum_type=FIROp)
FIRStatusField = EnumField.specialize(enum_type=FIRStatus)


class FIRCmd(DataList):
    """One matrix-FIR command (host -> ``s_in`` -> ``load``).  Addresses are **element (float)
    offsets** into the shared data region (owned by the driver's memory map)."""
    elements = {
        "op":     {"schema": FIROpField, "description": "FIROp (fir / end)"},
        "tx_id":  {"schema": Word32, "description": "transaction id (response correlation)"},
        "x_off":  {"schema": Addr32, "description": "input matrix base (element offset)"},
        "h_off":  {"schema": Addr32, "description": "FIR taps base (element offset)"},
        "y_off":  {"schema": Addr32, "description": "output matrix base (element offset)"},
        "n_rows": {"schema": Word32, "description": "matrix rows"},
        "n_cols": {"schema": Word32, "description": "matrix cols"},
    }


class FIRResp(DataList):
    """One matrix-FIR response (``store`` -> ``m_out`` -> host): tx_id echo + completion status
    (0 = ok, 1 = bad_size).  Per-job — a bad-size job is flagged without halting the pipeline."""
    elements = {
        "tx_id":  {"schema": Word32, "description": "echo of the command transaction id"},
        "status": {"schema": Word32, "description": "completion status (0 = ok, 1 = bad_size)"},
    }


class FirMeta(DataList):
    """Reduced per-job control record on the **internal** ``load -> compute -> store`` ctrl
    stream — only the fields ``compute`` / ``store`` need (NOT the load-only ``x_off`` / ``h_off``
    of :class:`FIRCmd`).  Serialized over a fixed-width 32-bit channel via the built-in
    serializers (``write_stream`` / ``read_stream``) — a genuinely narrow word channel, not a
    wide ``hls::stream<Cmd>`` struct FIFO."""
    elements = {
        "n_rows": {"schema": Word16, "description": "matrix rows"},
        "n_cols": {"schema": Word16, "description": "matrix cols"},
        "y_off":  {"schema": Addr32, "description": "output matrix base (element offset)"},
        "tx_id":  {"schema": Word16, "description": "transaction id"},
        "status": {"schema": FIRStatusField, "description": "ok / bad_size / end (drain marker)"},
    }


#: Schema classes the gen-include step emits headers for (consumed by fir_pipeline_impl.tpp).
SCHEMA_CLASSES = [FIROpField, FIRStatusField, FIRCmd, FIRResp, FirMeta]

#: The fitted streaming cycle model (``fir_calibrate.py`` -> from the cosim sweep).  The sim loads
#: it via :meth:`FIRTiming.from_calibration`; absent (a fresh build before calibration), the baked
#: defaults below keep the functional sim running (timing does not affect bit-exactness).
CALIB_JSON = Path(__file__).resolve().parent / "results" / "fir_calibration.json"


@dataclass
class FIRTiming:
    """Streaming free-running pipeline cycle model — **occupancy-based, near-fit-free**.

    The components are physical and deterministic, not fitted (the matrix-LT philosophy,
    ``project-matrix-lt-fir-build``):

    * **bus occupancy = transfer beats = nwords** (``beats == nwords``, validated in cosim) —
      the load moves ``read_words`` beats (X + taps), the store ``write_words`` beats (Y).  This
      is *exact*; the per-stage wall-clock *span* is NOT used (it is contaminated by stalls and
      interleaved beats of the other direction — measure occupancy, not span).
    * **compute II=1** — ``n_rows*n_cols`` input samples streamed through the shift-register FIR.

    The load and store **serialize on the one ``gmem`` bundle in practice** — the VCD shows their
    bursts overlap <10%, so per job the bus moves ``read_words + write_words`` beats *serially*,
    which is what makes the period ``≈ (read+write)*beta``.  This is NOT a bus limit: an isolation
    toy (``sandbox/duplex_toy``) shows one bundle IS full-duplex for both a single process AND two
    DATAFLOW processes (read+write cosim ≈ max, not sum) — so the serialization is a property of
    the FIR's dataflow *dynamics* (the load/store bursts don't coincide in time), i.e. ~2×
    throughput headroom.  The sim's shared-bus :class:`Resource` reproduces the measured
    serialization.  The single calibrated residual is:

    * ``beta`` — m_axi **sustained cyc/beat** (~1.45; the Vitis random-stall efficiency,
      cf. ``sandbox/freerun_notes.md``) — fit from period-vs-occupancy over a few points.

    ``bus_job_cyc`` (per-job address/command setup) and ``pipe_fill_cyc`` (the command stream-in +
    DATAFLOW fill/drain, a single-job lead-in that overlaps across jobs) are small constants the
    composition needs; the **end-to-end period/latency are emergent**, validated against cosim, not
    fitted target-by-target."""

    clk_ns: float = 10.0
    bus_beat_cyc: float = 1.44     # beta: m_axi sustained cyc/beat (the ONE calibrated residual)
    bus_job_cyc: float = 20.0      # per-job bus address/setup overhead (charged once, on load)
    compute_beat_cyc: float = 1.0  # II=1: cyc per input sample streamed through the FIR
    pipe_fill_cyc: float = 130.0   # command stream-in + DATAFLOW fill/drain (lead-in; overlaps)

    @classmethod
    def from_calibration(cls, path: Path = CALIB_JSON) -> "FIRTiming":
        """Load the fitted coefficients from *path*; fall back to the baked defaults if absent."""
        if not Path(path).exists():
            return cls()
        c = json.loads(Path(path).read_text(encoding="utf-8"))
        if "model_params" not in c:        # not the streaming calibration (e.g. a stale file)
            return cls()
        p = c["model_params"]
        return cls(clk_ns=float(c["clk_ns"]), bus_beat_cyc=p["bus_beat_cyc"],
                   bus_job_cyc=p["bus_job_cyc"], compute_beat_cyc=p["compute_beat_cyc"],
                   pipe_fill_cyc=p["pipe_fill_cyc"])

    # -- physical component models ------------------------------------------
    def bus_cyc(self, nwords: int) -> float:
        """Bus occupancy of an ``nwords``-beat transfer (beats * beta)."""
        return self.bus_beat_cyc * nwords

    def compute_cyc(self, n_rows: int, n_cols: int) -> float:
        """The streaming FIR compute: II=1 over the ``n_rows*n_cols`` input samples."""
        return self.compute_beat_cyc * n_rows * n_cols

    def sec(self, cyc: float) -> float:
        return max(0.0, cyc) * self.clk_ns * 1e-9


@dataclass
class FIRAccel(HwComponent):
    """Free-running streaming FIR accelerator: ``ap_ctrl_hs`` + ``axis`` ``s_in``/``m_out`` +
    ``m_axi`` ``m_mem``, wrapping the ``pipeline`` DATAFLOW hook (load/compute/store +
    shift-register FIR + per-job status), reproducing the validated sandbox."""

    cpp_kernel_name: ClassVar[str | None] = "fir"
    cpp_namespace: ClassVar[str | None] = "fir_impl"

    mem_dwidth: HwParam[int] = 32       # m_axi data width (float32 -> 1 word/element)
    mem_awidth: HwParam[int] = 32
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))  # 10 ns/cycle (matches cosim)

    def __post_init__(self) -> None:
        super().__post_init__()
        # Control is in-band on the streams (command on s_in, per-job response on m_out); the
        # data stays on the m_mem AXI-MM bundle.  No regmap -> ap_ctrl_hs top (run_proc extracted).
        self.s_in = StreamIFSlave(
            name=f"{self.name}_s_in", sim=self.sim, bitwidth=self.mem_dwidth)
        self.m_out = StreamIFMaster(
            name=f"{self.name}_m_out", sim=self.sim, bitwidth=self.mem_dwidth)
        self.m_mem = MMIFMaster(
            name=f"{self.name}_m_mem", sim=self.sim, bitwidth=self.mem_dwidth)
        for ep in (self.s_in, self.m_out, self.m_mem):
            self.add_endpoint(ep)
        # Attached by the driver (owns the system memory map) before run_sim.
        self.data_base: int = 0
        self.logger = NullLogger()
        self.events: list[dict] = []
        self._mem_bw = int(self.mem_dwidth)
        # The calibrated streaming cycle model (the freerun-cosim fit; baked defaults if absent).
        self.timing = FIRTiming.from_calibration()

    @property
    def Cmd(self) -> type[FIRCmd]:
        return FIRCmd

    # --- the golden ----------------------------------------------------------
    def execute(self, X: np.ndarray, h: np.ndarray) -> np.ndarray:
        """The pure bit-exact golden — the ONE shared ``fir_golden`` (no memory/timing)."""
        return fir_golden(X, h)

    # --- host-facing entry == the codegen marker -----------------------------
    def run_proc(self) -> ProcessGen[None]:
        """A single call to the ``pipeline`` hook — the codegen root.  ``extract_kernel`` (no
        regmap) extracts this ``run_proc`` and emits the ``ap_ctrl_hs`` top calling
        ``fir_impl::pipeline(s_in, m_out, m_mem)``."""
        yield from self.pipeline(self.s_in, self.m_out, self.m_mem)

    def _ev(self, event: str, tx_id: int, **extra) -> None:
        """Record a bus-visible timeline event (cycles), for the timing gate / figure."""
        self.events.append({"event": event, "tx_id": tx_id,
                            "t": self.now, "cyc": self.now / self.clk.period, **extra})

    @staticmethod
    def _is_valid(cmd: FIRCmd) -> bool:
        return T <= int(cmd.n_cols) <= NCOL_MAX and 1 <= int(cmd.n_rows) <= MAX_ROWS

    def _bus_xfer(self, occupancy_cyc: float, movement: ProcessGen) -> ProcessGen:
        """Acquire the shared ``gmem`` bus, run the functional *movement* (its bus time is
        **subsumed**), and hold the bus for the full *occupancy_cyc* (beats * beta).  The
        load-read and store-write of in-flight jobs SERIALIZE on this one resource (modeling the
        FIR's measured <10% load/store burst overlap — see :class:`FIRTiming`), so the bus moves
        ``read_words + write_words`` beats per job — the composition that sets the period."""
        with self._bus.request() as req:
            yield req
            t0 = self.now
            result = yield from movement
            elapsed = (self.now - t0) / self.clk.period
            yield self.timeout(self.timing.sec(occupancy_cyc - elapsed))
            return result

    # --- the synthesizable hook (codegen: fir_pipeline_impl.tpp; sim: this body) ---
    @synthesizable
    def pipeline(self, s_in: StreamIFSlave, m_out: StreamIFMaster,
                 mem: MMIFMaster) -> ProcessGen[None]:
        """The free-running streaming FIR.

        **Codegen** replaces this body with ``fir_pipeline_impl.tpp`` — the ``#pragma HLS
        DATAFLOW`` region (load / compute / store ``while(!done)`` stages, shift-register FIR at
        II=1, ``ap_uint<MEM_DW>*`` gmem via ``read_array_slice`` / ``write_array_slice``, END
        drain, per-job status).  ``mem`` lowers to the m_axi pointer (the hwgen m_axi-hook-arg
        branch); ``s_in`` / ``m_out`` to ``axis``.

        **Sim** — this body is the streaming model: spawn the three persistent stage processes
        (``load`` / ``compute`` / ``store``) wired by unbounded job queues (the sim twin of the
        DATAFLOW FIFOs) over **one shared ``gmem`` bus** (``self._bus``).  The dispatcher pulls
        each ``FIRCmd`` off ``s_in`` and hands it to ``load`` **without blocking**, so successive
        jobs overlap (``load(N+1) ∥ store(N)``); the bus resource serializes their read/write beats
        (matching the FIR's measured non-overlap) — that composition makes the throughput period
        emerge from the bus *occupancy*, not from a fitted end-to-end model.  The ``END`` sentinel drains
        the network (each stage forwards it and returns), mirroring the kernel's per-batch
        ``ap_done`` restart.  The functional movement + the shared ``fir_golden`` keep the sim's
        ``Y`` bit-exact with the kernel's; timing is the occupancy-based :class:`FIRTiming`."""
        data = mem.region(self.data_base, Float32, word_bw=self._mem_bw)
        self._bus = self.resource(capacity=1)   # the shared gmem bus (FIR serializes load/store)
        load_q = self.transaction_queue()       # dispatcher -> load
        comp_q = self.transaction_queue()       # load       -> compute
        store_q = self.transaction_queue()      # compute     -> store
        self.process(self._load_stage(data, load_q, comp_q))
        self.process(self._compute_stage(comp_q, store_q))
        self.process(self._store_stage(data, store_q, m_out))

        while True:
            cmd: FIRCmd = yield from s_in.get(FIRCmd)
            if int(cmd.op) == int(FIROp.end):
                # END drains behind the in-flight jobs' lead-in delays (preserve order).
                self.process(self._delayed_put(load_q, ("end", None), self.timing.pipe_fill_cyc))
                return
            self._ev("cmd_arrive", int(cmd.tx_id), n_rows=int(cmd.n_rows), n_cols=int(cmd.n_cols))
            # The command stream-in + DATAFLOW fill is a per-job LEAD-IN latency that overlaps
            # across jobs (it is NOT a throughput cost) -> a parallel delay, not a serial timeout.
            self.process(self._delayed_put(load_q, ("job", cmd), self.timing.pipe_fill_cyc))

    def _delayed_put(self, q, item, delay_cyc: float) -> ProcessGen[None]:
        """Put *item* on *q* after a *delay_cyc* lead-in — spawned per job so the lead-ins overlap
        (a pure latency, not a throughput bottleneck)."""
        yield self.timeout(self.timing.sec(delay_cyc))
        q.put(item)

    # --- the three persistent stage processes (sim-only; the kernel's DATAFLOW stages) ---
    def _load_stage(self, data, load_q, comp_q) -> ProcessGen[None]:
        """Read X + taps off the shared bus (``read_words`` beats + the per-job setup); forward to
        ``compute``.  A bad-size job streams no data (a balanced ``bad`` marker, like the kernel).
        The ``pipe_fill_cyc`` lead-in (command stream-in + DATAFLOW fill) precedes the read and
        overlaps across jobs (the dispatcher already queued the next command)."""
        while True:
            kind, cmd = yield load_q.get()
            if kind == "end":
                comp_q.put(("end", None, None))
                return
            tx = int(cmd.tx_id)
            if not self._is_valid(cmd):
                comp_q.put(("bad", cmd, None))
                continue
            n_rows, n_cols = int(cmd.n_rows), int(cmd.n_cols)
            read_words = n_rows * n_cols + T
            self._ev("load_begin", tx)

            def read_xh() -> ProcessGen:
                hf = yield from data.read_slice(int(cmd.h_off), int(cmd.h_off) + T)
                Xf = yield from data.read_slice(int(cmd.x_off), int(cmd.x_off) + n_rows * n_cols)
                return hf, Xf

            hf, Xf = yield from self._bus_xfer(
                self.timing.bus_cyc(read_words) + self.timing.bus_job_cyc, read_xh())
            self._ev("load_end", tx)
            X = np.asarray(Xf, dtype=np.float32).reshape(n_rows, n_cols)
            comp_q.put(("job", cmd, (X, np.asarray(hf, dtype=np.float32))))

    def _compute_stage(self, comp_q, store_q) -> ProcessGen[None]:
        """The streaming shift-register FIR (II=1 over the ``n_rows*n_cols`` input samples) — the
        ONE shared golden.  No bus; it runs under the bus shadow (the kernel is memory-bound), so
        it is hidden in steady state and only exposed in the single-job latency."""
        while True:
            kind, cmd, payload = yield comp_q.get()
            if kind == "end":
                store_q.put(("end", None, None))
                return
            if kind == "bad":
                store_q.put(("bad", cmd, None))
                continue
            tx = int(cmd.tx_id)
            n_rows, n_cols = int(cmd.n_rows), int(cmd.n_cols)
            X, h = payload
            self._ev("compute_begin", tx)
            Y = self.execute(X, h)
            yield self.timeout(self.timing.sec(self.timing.compute_cyc(n_rows, n_cols)))
            self._ev("compute_end", tx)
            store_q.put(("job", cmd, Y))

    def _store_stage(self, data, store_q, m_out) -> ProcessGen[None]:
        """Write Y over the shared bus (``write_words`` beats — serialized with the loads of
        in-flight jobs on the one bundle) + emit the per-job response on ``m_out`` (``ok`` /
        ``bad_size``); the ``END`` sentinel ends the batch."""
        while True:
            kind, cmd, Y = yield store_q.get()
            if kind == "end":
                return
            tx = int(cmd.tx_id)
            if kind == "bad":
                yield from m_out.write(FIRResp(tx_id=tx, status=int(FIRStatus.bad_size)))
                self._ev("resp_sent", tx, status=int(FIRStatus.bad_size))
                continue
            n_rows = int(cmd.n_rows)
            write_words = n_rows * (int(cmd.n_cols) - T + 1)
            self._ev("store_begin", tx)

            def write_y() -> ProcessGen:
                yield from data.write_slice(int(cmd.y_off), Y.ravel(), element_type=Float32)

            yield from self._bus_xfer(self.timing.bus_cyc(write_words), write_y())
            self._ev("store_end", tx)
            yield from m_out.write(FIRResp(tx_id=tx, status=int(FIRStatus.ok)))
            self._ev("resp_sent", tx, status=int(FIRStatus.ok))
