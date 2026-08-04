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

**Stage-B sim = the streaming (double-buffered) pipeline.**  The hook's sim body spawns **three
persistent stage processes** (``load`` / ``compute`` / ``store``) wired by unbounded job queues —
the sim twin of the kernel's three ``#pragma HLS DATAFLOW`` ``while(!done)`` stages.  ``run_proc``
pulls each command off ``s_in`` and kicks ``load`` **without blocking**, so successive jobs overlap
(``load(N+1) ∥ store(N)``).  The load's X-read and the store's Y-write use the **anchored** Region
transfers (``read_slice_pipelined`` / ``write_slice_pipelined``): they hold the memory slave's
**separate** AR/R and AW/W channels (full-duplex, per the lane-loop fix), and the store is
**early-anchored** at ``x_read_start + t_fill`` (the X-read burst start, *after* the h-read holds
the R channel) with ``min_span = compute_body`` so its Y-write
*overlaps* the X-read (within-job streaming) and hides under compute — the throughput **period**
then emerges from the busier direction's channel *occupancy*, not a fitted end-to-end model.

**Two-level timing (see [[project-two-level-calibration]]).**  The per-direction bus spans are the
memory slave's :class:`~waveflow.hw.memif.BusTiming` — the **platform** bus characterization
(``setup``/``per_word``), configured in ``fir_sim.py`` and reused by any accelerator; the component
supplies only ``num_trans``/``nwords``.  The **compute** — the one thing calibrated *per kernel* — is
two learned :class:`~waveflow.calib.LinCalibModel`s (``fill_model``, ``compute_model``) held by the
accelerator, so the sim reads ``model.predict_feat(row)`` right where the estimate is used.  If both the
bus model and the compute fit are right, the loaded end-to-end throughput matches with zero
end-to-end fitting (Gate B).  The functional truth (``execute``) is bit-exact-shared with the
kernel; only the timing is modeled.

**⚠ COMPUTE PARAMS PROVISIONAL (streaming re-cal, 2026-07-02).**  The compute models describe the
FIXED (lane-loop) kernel — ~1.5× faster (704→473 cyc/job @ 4×64), full-duplex, streaming *within* a
job.  They **supersede** the old buffered/occupancy model (which serialized load/store, β≈1.45).
The seed coeffs are un-calibrated placeholders; run a
contention-free freerun sweep + ``fir_calibrate.py`` to fit ``streaming_params`` (compute only) and
validate Gate B before trusting the reported cycles.  **The functional golden is unaffected.**  See
[[project-fir-slice-vs-laneloop-rootcause]] and ``plans/fir_freerun_integration.md``.
"""
from __future__ import annotations

import enum
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

from waveflow.calib import LinCalibModel
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataList, EnumField, FloatField, IntField, MemAddr
from waveflow.hw.hw_module import HwModule, HwParam
from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
from waveflow.hw.memif import MMIFMaster
from waveflow.hw.synth import synthesizable
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

#: Per-model COMPUTE artifacts (one file each — the BuildDAG tracks them).  ``fir_calibrate.py``
#: fits + ``save_model()``s them from a contention-free cosim sweep; absent (a fresh build before
#: calibration), the seed coeffs below keep the functional sim running (timing ≠ bit-exactness).
RESULTS = Path(__file__).resolve().parent / "results"
FILL_MODEL_PATH = RESULTS / "fir_fill_model.json"
COMPUTE_MODEL_PATH = RESULTS / "fir_compute_model.json"


# -- the per-kernel COMPUTE model (the ONLY thing calibrated per accelerator) --------------------
# Two through-origin :class:`~waveflow.calib.LinCalibModel`s, each a learned estimate of a TIME (so
# the sim reads ``model.predict_feat(row)`` directly and it's obvious where the estimate is made).  Bus
# transfer time is NOT here — it's the platform's, configured on the memory slave in fir_sim.py.
# See [[project-two-level-calibration]].  ``clk_period`` is folded into the features, so the fit is
# clock-independent (coeffs are cycle-domain rates) and predict returns seconds:
#   fill_time    = clk_period · L0                                  (ramp to first Y output)
#   compute_time = clk_period · [row_setup·(n_row-1) + beat·n_row·n_col]   (II=1 body + per-row flush)
# The ``ROWS`` loop is not flattened/rewound (``fir_pipeline_impl.tpp``), so each of the n_row-1
# non-first rows pays the inner-pipeline flush ``row_setup``; the first row's fill is L0.

def _fill_features(row: dict) -> list[float]:
    """Fill-time feature: ``[clk_period]`` (coeff L0 = ramp cycles)."""
    return [float(row["clk_period"])]


def _compute_features(row: dict) -> list[float]:
    """Compute-time features: ``[clk_period·(n_row-1), clk_period·n_row·n_col]`` (coeffs
    row_setup, compute_beat — cycle-domain rates)."""
    cp, nr, nc = float(row["clk_period"]), float(row["n_row"]), float(row["n_col"])
    return [cp * (nr - 1.0), cp * nr * nc]


#: Provisional seed coeffs (cycles) — the default until fir_calibrate.py fits + save_model()s.
_FILL_SEED = {"L0": 60.0}
_COMPUTE_SEED = {"row_setup": 8.0, "compute_beat": 1.0}


def make_fill_model() -> LinCalibModel:
    """The fill-time model shell (seed + artifact path); fit or ``load_or_default()``."""
    return LinCalibModel(basis=[], target="fill_time", fit_intercept=False,
                         transform_fn=_fill_features, coeff_names=["L0"],
                         seed=_FILL_SEED, path=FILL_MODEL_PATH)


def make_compute_model() -> LinCalibModel:
    """The compute-time model shell (seed + artifact path); fit or ``load_or_default()``."""
    return LinCalibModel(basis=[], target="compute_time", fit_intercept=False,
                         transform_fn=_compute_features, coeff_names=["row_setup", "compute_beat"],
                         seed=_COMPUTE_SEED, path=COMPUTE_MODEL_PATH)


def load_compute_models() -> tuple[LinCalibModel, LinCalibModel]:
    """The ``(fill_model, compute_model)`` for the sim — each loads its fitted artifact
    (``load_or_default``) or falls back to its seed (deployed: no sklearn / data at sim time)."""
    return make_fill_model().load_or_default(), make_compute_model().load_or_default()


@dataclass
class FIRAccel(HwModule):
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
        self.events: list[dict] = []
        self._mem_bw = int(self.mem_dwidth)
        # The per-kernel COMPUTE models (fill/compute time; freerun-cosim fit, seeds if absent) —
        # explicit learned estimates.  Bus transfer time is the platform's (memory slave, by driver).
        self.fill_model, self.compute_model = load_compute_models()

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

    def _ev(self, event: str, tx_id: int, t: float | None = None, **extra) -> None:
        """Record a bus-visible timeline event (cycles).  *t* overrides ``now`` — the store's
        Y-write is early-anchored, so ``store_begin`` / ``store_end`` log the effective span
        endpoints (``t0`` / ``t1``), not the late resource-acquire time."""
        tt = self.now if t is None else t
        self.events.append({"event": event, "tx_id": tx_id,
                            "t": tt, "cyc": tt / self.clk.period, **extra})

    @staticmethod
    def _is_valid(cmd: FIRCmd) -> bool:
        return T <= int(cmd.n_cols) <= NCOL_MAX and 1 <= int(cmd.n_rows) <= MAX_ROWS

    # --- the synthesizable hook (codegen: fir_pipeline_impl.tpp; sim: this body) ---
    @synthesizable
    def pipeline(self, s_in: StreamIFSlave, m_out: StreamIFMaster,
                 mem: MMIFMaster) -> ProcessGen[None]:
        """The free-running streaming FIR.

        **Codegen** replaces this body with ``fir_pipeline_impl.tpp`` — the ``#pragma HLS
        DATAFLOW`` region (load / compute / store ``while(!done)`` stages, shift-register FIR at
        II=1, ``ap_uint<MEM_DW>*`` gmem streamed direct (the lane-loop fix), END drain, per-job
        status).  ``mem`` lowers to the m_axi pointer; ``s_in`` / ``m_out`` to ``axis``.

        **Sim — streaming (double-buffered) model.**  Spawn the three persistent stage processes
        (``load`` / ``compute`` / ``store``) wired by unbounded job queues.  The load's X-read and
        the store's Y-write use the **anchored** Region transfers (``read_slice_pipelined`` /
        ``write_slice_pipelined``): they hold the memory slave's **separate** AR/R and AW/W
        channels (full-duplex — the lane-loop fix enabled it), and the store is **early-anchored**
        at ``x_read_start + t_fill`` (the X-read burst start, after the h-read) with ``min_span =
        compute_body`` so its Y-write *overlaps* the X-read (within-job streaming) and hides under
        compute.  The per-direction channel spans are
        the memory slave's calibrated :class:`~waveflow.hw.memif.BusTiming` (attached by the
        driver); the component supplies only ``num_trans`` at the slice call.  The dispatcher pulls
        each ``FIRCmd`` off ``s_in`` and kicks ``load`` without blocking, so ``load(N+1) ∥
        store(N)``.  The ``END`` sentinel drains the network.  ``execute`` (the shared golden) keeps
        ``Y`` bit-exact; the compute timing is the two learned models (PROVISIONAL until calibrated)."""
        data = mem.region(self.data_base, Float32, word_bw=self._mem_bw)
        load_q = self.transaction_queue()       # dispatcher -> load
        comp_q = self.transaction_queue()       # load       -> compute
        store_q = self.transaction_queue()      # compute     -> store
        self.process(self._load_stage(data, load_q, comp_q))
        self.process(self._compute_stage(comp_q, store_q))
        self.process(self._store_stage(data, store_q, m_out))

        while True:
            t_cmd = self.now                    # command-write time: the fill/latency reference
            cmd: FIRCmd = yield from s_in.get(FIRCmd)
            if int(cmd.op) == int(FIROp.end):
                load_q.put(("end", None, None))
                return
            self._ev("cmd_arrive", int(cmd.tx_id), n_rows=int(cmd.n_rows), n_cols=int(cmd.n_cols))
            load_q.put(("job", cmd, t_cmd))     # kick load without blocking -> jobs overlap

    # --- the three persistent stage processes (sim-only; the kernel's DATAFLOW stages) ---
    def _load_stage(self, data, load_q, comp_q) -> ProcessGen[None]:
        """Read h (taps) + X off the AR/R channel via anchored ``read_slice_pipelined`` (one burst
        each, ``num_trans=1``) — this models the R-channel OCCUPANCY (for throughput).  The fill
        anchor forwarded to ``compute`` is ``t_cmd`` (the command-write time), NOT the X-read start:
        the fill absorbs all initial-processing latency (command decode + the h-read + the X ramp +
        FP fill), so the h-read's setup-heavy cost doesn't need separate modeling.  A bad-size job
        streams no data (a balanced ``bad`` marker, like the kernel)."""
        while True:
            kind, cmd, t_cmd = yield load_q.get()
            if kind == "end":
                comp_q.put(("end", None, None))
                return
            tx = int(cmd.tx_id)
            if not self._is_valid(cmd):
                comp_q.put(("bad", cmd, None))
                continue
            n_rows, n_cols = int(cmd.n_rows), int(cmd.n_cols)
            self._ev("load_begin", tx)
            hf, _ = yield from data.read_slice_pipelined(
                int(cmd.h_off), int(cmd.h_off) + T, t_out_start=self.now, num_trans=1)
            Xf, _ = yield from data.read_slice_pipelined(   # X after h on the same R channel
                int(cmd.x_off), int(cmd.x_off) + n_rows * n_cols, num_trans=1)
            self._ev("load_end", tx)
            X = np.asarray(Xf, dtype=np.float32).reshape(n_rows, n_cols)
            comp_q.put(("job", cmd, (X, np.asarray(hf, dtype=np.float32), t_cmd)))

    def _compute_stage(self, comp_q, store_q) -> ProcessGen[None]:
        """The streaming shift-register FIR (II=1) — the ONE shared golden.  Holds **no** bus
        channel (so it overlaps load(N+1)/store(N)); its timing is carried into the store's
        anchored span: the store's early anchor ``t_cmd + t_fill`` and its compute-shadow floor
        ``min_span = compute_body`` (the II=1 production span)."""
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
            X, h, t_cmd = payload
            self._ev("compute_begin", tx)
            Y = self.execute(X, h)
            # The two learned COMPUTE estimates (seconds; clk_period folded into the model features).
            # t_fill = ramp from the command-write time to the first Y output (absorbs cmd decode +
            # h-read + X ramp + FP fill); compute_body = the II=1 production span the store then hides
            # under (the write's min_span floor).
            row = {"n_row": n_rows, "n_col": n_cols, "clk_period": self.clk.period}
            t_store_anchor = t_cmd + self.fill_model.predict_feat(row)
            compute_body = self.compute_model.predict_feat(row)
            self._ev("compute_end", tx)
            store_q.put(("job", cmd, (Y, t_store_anchor, compute_body)))

    def _store_stage(self, data, store_q, m_out) -> ProcessGen[None]:
        """Early-anchored Y-write on the AW/W channel via ``write_slice_pipelined`` — anchored at
        ``t_store_anchor`` (when the first Y row was ready), occupying the channel for
        ``max(channel_occupancy, compute_body)``, so it OVERLAPS the X-read and hides under compute.
        Back-to-back writes serialize on the channel inside the bus model (the slave's
        ``write_busy_until``), so this stage just passes its *desired* early anchor.  Then emit the
        per-job response; the ``END`` sentinel ends the batch."""
        while True:
            kind, cmd, payload = yield store_q.get()
            if kind == "end":
                return
            tx = int(cmd.tx_id)
            if kind == "bad":
                yield from m_out.write(FIRResp(tx_id=tx, status=int(FIRStatus.bad_size)))
                self._ev("resp_sent", tx, status=int(FIRStatus.bad_size))
                continue
            Y, t_store_anchor, compute_body = payload
            t0, t1 = yield from data.write_slice_pipelined(
                int(cmd.y_off), Y.ravel(), t_out_start=t_store_anchor, num_trans=1,
                min_span=compute_body, element_type=Float32)
            self._ev("store_begin", tx, t=t0)
            self._ev("store_end", tx, t=t1)
            # the response write is a real transfer on m_out — its cost is modeled by the stream
            # interface itself (StreamIF._push_to_endpoint advances the clock by nwords), serial in
            # the store stage exactly like the kernel; no separate delay to add here.
            yield from m_out.write(FIRResp(tx_id=tx, status=int(FIRStatus.ok)))
            self._ev("resp_sent", tx, status=int(FIRStatus.ok))
