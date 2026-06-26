"""fir.py — free-running streaming FIR accelerator (Waveflow integration, Stage A).

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
via ``read_array_slice``/``write_array_slice``).  **In sim the hook body IS the model** —
Stage A: a functional, bit-exact batch processor with placeholder timing; Stage B replaces it
with the three persistent processes + the calibrated streaming timing.
"""
from __future__ import annotations

import enum
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

        **Sim (Stage A)** — this body is the functional model: read each ``FIRCmd`` off ``s_in``;
        ``END`` -> return; a bad-size job emits an error response and the loop continues (no
        halt); a good job reads ``X`` / ``h`` via the element-coordinate Region, runs the shared
        ``fir_golden``, writes ``Y``, and emits an ``ok`` response.  Bit-exact; placeholder
        timing (the calibrated streaming timing — three persistent processes — is Stage B)."""
        data = mem.region(self.data_base, Float32, word_bw=self._mem_bw)
        while True:
            cmd: FIRCmd = yield from s_in.get(FIRCmd)
            if int(cmd.op) == int(FIROp.end):
                return
            n_rows, n_cols = int(cmd.n_rows), int(cmd.n_cols)
            if not (T <= n_cols <= NCOL_MAX and 1 <= n_rows <= MAX_ROWS):
                yield from m_out.write(FIRResp(tx_id=int(cmd.tx_id),
                                               status=int(FIRStatus.bad_size)))
                continue
            hf = yield from data.read_slice(int(cmd.h_off), int(cmd.h_off) + T)
            Xf = yield from data.read_slice(int(cmd.x_off), int(cmd.x_off) + n_rows * n_cols)
            X = np.asarray(Xf, dtype=np.float32).reshape(n_rows, n_cols)
            Y = self.execute(X, np.asarray(hf, dtype=np.float32))
            yield from data.write_slice(int(cmd.y_off), Y.ravel(), element_type=Float32)
            yield from m_out.write(FIRResp(tx_id=int(cmd.tx_id), status=int(FIRStatus.ok)))
