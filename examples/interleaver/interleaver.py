"""interleaver.py — the canonical six-stage load-compute-store interleaver (the final generated shape).

The teachable anatomy AND the nj=8 fix: a free-running (ap_ctrl_none) ``hls::task`` network generated
by :func:`~waveflow.build.composite_gen.composite_top_spec` in which one ``InterleaverCmd`` token
per job is emitted by ``cmd_rx`` and read-then-forwarded by every stage, pacing each tile to one job in
flight (sob3's structure) so the pipeline never fills to the ``done == #tasks + 1`` depth the earlier
stream/SOB-mix and P-SOB variants deadlocked at.  XSI-verified bit-exact ``Y[i]=X[P[i]]`` at nj=8 (8/8)
and nj=16 (16/16); steady-state 414 cyc/job (the per-job token pacing trades some load/compute overlap
for the unbounded robustness)::

    s_cmd -> cmd_rx -> il_mem_r -> il_load -> il_compute -> il_store -> il_mem_w -> s_done
                      (token threaded through all six; data + block edges alongside)

Six sub-components wired by five Cmd ``StreamEdge``s (the forwarded token) + three data ``StreamEdge``s
(pwords, xwords, ywords) + three ``SobEdge``s (p_blk, x_blk, y_blk) + two ``m_axi`` bundles (gmem0 read
/ gmem1 write) + two AXIS boundary ports.  Element-coordinate / word_index throughout; the word block
is ``ap_uint<MEM_DW>[n/LW]`` and ``il_compute`` does LW random ``elem_read<MEM_DW>`` reads/cycle.  The
split count NW = n/LW is a compile-time constant baked into every tile from the one generate() param.

**Timing.**  ``414`` is the XSI-measured RTL steady state.  The loosely-timed pysim reproduces that
shape by charging two models: the platform's shipped **bus law** on the memory (the ``m_axi`` transfer
cost, reused from mem_copy — pass ``platform_dir`` to :func:`interleaver_sim.run_interleaver`), and the
custom gather's **loop model** on :class:`IlCompute` (``cycles = latency + ii·(nw − 1)``, seeded until a
cosim sweep fits it — see ``calibrate_compute.py``).  Rendering the per-stage ``fire_log`` on an
:class:`~waveflow.utils.timing.ActivityDiagram` (``interleaver_figures.py``) shows the pipeline overlap.

Run (project venv, from repo root)::

    PYTHONPATH=. pysilicon-venv/Scripts/python.exe examples/interleaver/interleaver.py
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

HERE = Path(__file__).resolve().parent

from waveflow.build.build import BuildConfig, BuildDag  # noqa: E402
from waveflow.build.streamutils import (  # noqa: E402
    MemMgrStep,
    MemStreamStep,
    StreamUtilsStep,
    XsiHarnessStep,
)
from waveflow.hw.arrayutils import gen_array_utils  # noqa: E402
from waveflow.hw.clock import Clock  # noqa: E402
from waveflow.hw.dataschema import DataArray, DataList, DataSchemaStep, IntField  # noqa: E402
from waveflow.hw.hw_component import HwParam  # noqa: E402
from waveflow.hw.hw_freerun import FreeRunComp  # noqa: E402
from waveflow.hw.interface import (  # noqa: E402
    SobIFMaster,
    SobIFSlave,
    StreamOfBlocksIF,
    StreamIF,
    StreamIFMaster,
    StreamIFSlave,
)
from waveflow.hw.memif import MMIFReadMaster, MMIFWriteMaster  # noqa: E402
from waveflow.hw.mem_stream import KernelTask, WORD_BW_SUPPORTED  # noqa: E402
from waveflow.simulation.simobj import ProcessGen  # noqa: E402

from waveflow.build.composite_gen import (  # noqa: E402
    GEN_DIR,
    INCLUDE_DIR,
    composite_top_spec,
    render_ports_h,
    render_tcl,
    render_top,
)

DEFAULT_MEM_DW = 64
DEFAULT_N = 256

# The block element type (32-bit, unsigned): its array-utils header supplies elem_read<MEM_DW> for the
# word-granular Gather.  The class name fixes the header/namespace to il_elem_array_utils(.h).
#
# It is a distinct *subclass* rather than ``specialize(...).__name__ = "IlElem"``: ``specialize`` returns
# a cached class keyed by (bitwidth, signed, include_dir), so that shared object is the very same
# ``UInt32`` another example (shared_mem/hist) specializes with the same key — renaming it in place would
# corrupt that example's codegen (its array-utils namespace derives from ``__name__``).  A subclass gets
# its own name while inheriting bitwidth/signed/include_dir/cpp_type untouched.
class IlElem(IntField.specialize(bitwidth=32, signed=False, include_dir=INCLUDE_DIR)):  # type: ignore[misc]
    pass

# --- command field type (element/word coordinates — the word_index convention) --------------------
Word32 = IntField.specialize(bitwidth=32, signed=False)


def _make_word_block(mem_dwidth: int, nw: int) -> type:
    """Create a typed WordBlock: DataArray of mem_dwidth-bit words, up to nw elements."""
    word_elem = IntField.specialize(bitwidth=int(mem_dwidth), signed=False)
    return DataArray.specialize(
        element_type=word_elem,
        max_shape=(int(nw),),
        member_name="words"
    )


class InterleaverCmd(DataList):
    """One app interleaver command (host -> ``s_cmd``): gather ``n`` elements ``Y[i]=X[P[i]]`` where
    P lives at word offset ``p_off``, X at ``x_off``, Y at ``y_off`` (all element/word coordinates)."""
    include_filename: ClassVar[str | None] = "il_cmd.h"
    elements = {
        "p_off": {"schema": Word32, "description": "P (index) buffer word offset"},
        "x_off": {"schema": Word32, "description": "X (source) buffer word offset"},
        "y_off": {"schema": Word32, "description": "Y (output) buffer word offset"},
        "n":     {"schema": Word32, "description": "number of elements to interleave"},
    }


#: Schema classes the gen-include step emits C++ headers for (the canonical tiles use only the token).
SCHEMA_CLASSES = [InterleaverCmd]


def _nwords(n: int, lw: int) -> int:
    """Words per array: LW 32-bit elements per MEM_DW word (ceil)."""
    return (n + lw - 1) // lw


# ---------------------------------------------------------------------------
# Canonical six-stage variant — a forwarded per-job token through every tile
# ---------------------------------------------------------------------------
#
# The teachable load-compute-store anatomy AND the nj=8 deadlock fix: one InterleaverCmd token per job
# is emitted by cmd_rx and forwarded through every stage (five Cmd StreamEdges), so each tile is paced
# to one job in flight (sob3's structure) — the pipeline never fills to the done==#tasks+1 depth the
# mix / P-SOB variants hit.  mem_w emits the token on s_done AFTER the write burst (commit-timed done).
#
#   cmd_rx -> il_mem_r -> il_load -> il_compute -> il_store -> il_mem_w -> s_done
#            (token threaded through all six; data edges alongside)

_TOKEN = InterleaverCmd


def _word_t(mem_dwidth: int) -> type:
    return IntField.specialize(bitwidth=int(mem_dwidth), signed=False)


@dataclass
class CmdRx(FreeRunComp):
    """Stage 1: read the app command off the ``s_cmd`` AXIS boundary and emit the per-job token."""

    cpp_kernel_name: ClassVar[str | None] = "cmd_rx"
    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        self.s_cmd = StreamIFSlave(name=f"{self.name}_s_cmd", sim=self.sim, bitwidth=w,
                                   has_tlast=False)
        self.cmd_out = StreamIFMaster(name=f"{self.name}_cmd_out", sim=self.sim, bitwidth=w,
                                      has_tlast=False)
        for ep in (self.s_cmd, self.cmd_out):
            self.add_endpoint(ep)
        self.fire_log: list[tuple[float, float]] = []   # per-firing (start, end) cycles

    def kernel_task(self) -> KernelTask:
        return KernelTask("cmd_rx_task", "cmd_rx_task.h", ("s_cmd", "cmd_out"),
                          template_args=(int(self.mem_dwidth),))

    def run_iter(self) -> ProcessGen[None]:
        cmd = yield from self.s_cmd.get(_TOKEN)
        t0 = self.now
        yield from self.cmd_out.write(cmd)
        self.fire_log.append((t0 / self.clk.period, self.now / self.clk.period))


@dataclass
class IlMemR(FreeRunComp):
    """Stage 2 (m_axi read owner, gmem0): token -> burst P->pwords + X->xwords -> forward token."""

    cpp_kernel_name: ClassVar[str | None] = "il_mem_r"
    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    n: HwParam[int] = DEFAULT_N
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        self.lw = w // 32
        self.nw = _nwords(int(self.n), self.lw)
        self.cmd_in = StreamIFSlave(name=f"{self.name}_cmd_in", sim=self.sim, bitwidth=w,
                                    has_tlast=False)
        # The sole m_axi READ owner -- the type declares it, so the const pointer + stable pragma
        # derive from here rather than from a `kind` string in the composite's boundary.
        self.m_mem = MMIFReadMaster(name=f"{self.name}_m_mem", sim=self.sim, bitwidth=w)
        self.cmd_out = StreamIFMaster(name=f"{self.name}_cmd_out", sim=self.sim, bitwidth=w,
                                      has_tlast=False)
        self.pwords = StreamIFMaster(name=f"{self.name}_pwords", sim=self.sim, bitwidth=w,
                                     has_tlast=False)
        self.xwords = StreamIFMaster(name=f"{self.name}_xwords", sim=self.sim, bitwidth=w,
                                     has_tlast=False)
        for ep in (self.cmd_in, self.m_mem, self.cmd_out, self.pwords, self.xwords):
            self.add_endpoint(ep)
        self._wt, self._bw = _word_t(w), w
        self.fire_log: list[tuple[float, float]] = []

    def kernel_task(self) -> KernelTask:
        return KernelTask("il_mem_r_task", "il_mem_r_task.h",
                          ("cmd_in", "m_mem", "cmd_out", "pwords", "xwords"),
                          template_args=(int(self.mem_dwidth), self.nw))

    def run_iter(self) -> ProcessGen[None]:
        nw = self.nw
        cmd = yield from self.cmd_in.get(_TOKEN)
        t0 = self.now
        yield from self.cmd_out.write(cmd)
        region = self.m_mem.region(0, self._wt, word_bw=self._bw)
        pw = int(cmd.p_off)
        pdata, _ = yield from region.read_slice_pipelined(pw, pw + nw)
        yield from self.pwords.write(np.asarray(pdata))
        xw = int(cmd.x_off)
        xdata, _ = yield from region.read_slice_pipelined(xw, xw + nw)
        yield from self.xwords.write(np.asarray(xdata))
        self.fire_log.append((t0 / self.clk.period, self.now / self.clk.period))


@dataclass
class IlLoad(FreeRunComp):
    """Stage 3 (stream->SOB): token + pwords/xwords -> fill p_blk/x_blk -> forward token."""

    cpp_kernel_name: ClassVar[str | None] = "il_load"
    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    n: HwParam[int] = DEFAULT_N
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        self.lw = w // 32
        self.nw = _nwords(int(self.n), self.lw)
        self.cmd_in = StreamIFSlave(name=f"{self.name}_cmd_in", sim=self.sim, bitwidth=w,
                                    has_tlast=False)
        self.pwords = StreamIFSlave(name=f"{self.name}_pwords", sim=self.sim, bitwidth=w,
                                    has_tlast=False)
        self.xwords = StreamIFSlave(name=f"{self.name}_xwords", sim=self.sim, bitwidth=w,
                                    has_tlast=False)
        self.cmd_out = StreamIFMaster(name=f"{self.name}_cmd_out", sim=self.sim, bitwidth=w,
                                      has_tlast=False)
        word_block = _make_word_block(w, self.nw)
        self.p_blk = SobIFMaster(name=f"{self.name}_p_blk", sim=self.sim, element_type=word_block)
        self.x_blk = SobIFMaster(name=f"{self.name}_x_blk", sim=self.sim, element_type=word_block)
        for ep in (self.cmd_in, self.pwords, self.xwords, self.cmd_out, self.p_blk, self.x_blk):
            self.add_endpoint(ep)
        self._dtype = np.uint32 if w <= 32 else np.uint64
        self.fire_log: list[tuple[float, float]] = []

    def kernel_task(self) -> KernelTask:
        return KernelTask("il_load_task", "il_load_task.h",
                          ("cmd_in", "pwords", "xwords", "cmd_out", "p_blk", "x_blk"),
                          template_args=(int(self.mem_dwidth), self.nw))

    def run_iter(self) -> ProcessGen[None]:
        nw = self.nw
        cmd = yield from self.cmd_in.get(_TOKEN)
        t0 = self.now
        yield from self.cmd_out.write(cmd)
        pblock = yield from self.p_blk.acquire_write()
        pw = yield from self.pwords.get(nwords_max=nw)
        pblock[:pw.shape[0]] = pw.astype(self._dtype)
        yield from self.p_blk.commit_write(pblock)
        xblock = yield from self.x_blk.acquire_write()
        xw = yield from self.xwords.get(nwords_max=nw)
        xblock[:xw.shape[0]] = xw.astype(self._dtype)
        yield from self.x_blk.commit_write(xblock)
        self.fire_log.append((t0 / self.clk.period, self.now / self.clk.period))


#: The compute task-body id — the key its firings carry and its subdir in the platform library (a
#: calibrated compute residual is stored under ``components/il_compute_task/``).
IL_COMPUTE_COMPONENT = "il_compute_task"

#: Seed loop-model parameters for the gather compute, ``cycles = latency + ii·(nw − 1)`` — the pipelined
#: word loop's fixed latency and its per-word initiation interval.  Placeholders until a cosim sweep
#: fits them (see the calibration guide); with them the loosely-timed pysim already charges a plausible
#: compute cost instead of zero.
IL_COMPUTE_LATENCY_SEED = 32.0
IL_COMPUTE_II_SEED = 2.0


@dataclass
class IlCompute(FreeRunComp):
    """Stage 4 (pure SOB->SOB): token + read-lock p_blk/x_blk -> gather into y_blk -> forward token.

    Unlike the framework mem-stream stages, this is a **custom compute** kernel — the interleaver's own
    gather — so its timing is not shipped: it carries its own loop model (``cycles = latency + ii·(nw −
    1)``) and charges that delay in ``run_iter``.  The model is seeded (:data:`IL_COMPUTE_LATENCY_SEED` /
    :data:`IL_COMPUTE_II_SEED`); point ``calib_dir`` at a fitted ``params.json`` to load a cosim-measured
    one instead.  This is the "fit your own component" half of the calibration story mem_copy has none
    of.
    """

    cpp_kernel_name: ClassVar[str | None] = "il_compute"
    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    n: HwParam[int] = DEFAULT_N
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    #: Optional directory holding a fitted ``params.json`` for the loop model (``nw`` + ``intercept``).
    #: ``None`` uses the seed.
    calib_dir: "str | None" = None

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        self.lw = w // 32
        self.nw = _nwords(int(self.n), self.lw)
        self.cmd_in = StreamIFSlave(name=f"{self.name}_cmd_in", sim=self.sim, bitwidth=w,
                                    has_tlast=False)
        word_block = _make_word_block(w, self.nw)
        self.p_blk = SobIFSlave(name=f"{self.name}_p_blk", sim=self.sim, element_type=word_block)
        self.x_blk = SobIFSlave(name=f"{self.name}_x_blk", sim=self.sim, element_type=word_block)
        self.cmd_out = StreamIFMaster(name=f"{self.name}_cmd_out", sim=self.sim, bitwidth=w,
                                      has_tlast=False)
        self.y_blk = SobIFMaster(name=f"{self.name}_y_blk", sim=self.sim, element_type=word_block)
        for ep in (self.cmd_in, self.p_blk, self.x_blk, self.cmd_out, self.y_blk):
            self.add_endpoint(ep)
        self.timing = self._build_timing_model()
        # The completion timeline (kept for back-compat) plus the per-firing start/span the activity
        # diagram and the calibration collector read.
        self.job_start_cyc: list[float] = []
        self.job_end_cyc: list[float] = []
        self.job_span_cyc: list[float] = []
        self.fire_log: list[tuple[float, float]] = []   # (start, end) cycles — uniform with the other stages

    def _build_timing_model(self):
        """A `latency + ii·(nw − 1)` loop model as a linear model in ``nw``: ``cycles = ii·nw +
        (latency − ii)``.  Loads a fitted ``params.json`` from ``calib_dir`` if present, else seeds."""
        from waveflow.calib.calib import LinCalibModel

        seed = {"nw": IL_COMPUTE_II_SEED,
                "intercept": IL_COMPUTE_LATENCY_SEED - IL_COMPUTE_II_SEED}
        path = None if self.calib_dir is None else Path(self.calib_dir) / "params.json"
        model = LinCalibModel(basis=["nw"], target="cycles", fit_intercept=True,
                              coeff_names=["nw"], seed=seed, path=path)
        model.load_or_default()      # load the fitted params if present, else the seed → ready to predict
        return model

    def kernel_task(self) -> KernelTask:
        return KernelTask("il_compute_task", "il_compute_task.h",
                          ("cmd_in", "p_blk", "x_blk", "cmd_out", "y_blk"),
                          template_args=(int(self.mem_dwidth), self.nw))

    def run_iter(self) -> ProcessGen[None]:
        lw, nw = self.lw, self.nw
        cmd = yield from self.cmd_in.get(_TOKEN)
        yield from self.cmd_out.write(cmd)
        pblock = yield from self.p_blk.acquire_read()
        xblock = yield from self.x_blk.acquire_read()
        yblock = yield from self.y_blk.acquire_write()
        t0 = self.now
        for w in range(nw):
            pword = int(pblock[w])
            yword = 0
            for lane in range(lw):
                idx = (pword >> (32 * lane)) & 0xFFFFFFFF
                xword = int(xblock[idx // lw])
                xv = (xword >> (32 * (idx % lw))) & 0xFFFFFFFF
                yword |= xv << (32 * lane)
            yblock[w] = yword
        # The gather is instantaneous in pysim; charge the cycles the pipelined word loop would take in
        # hardware, holding the read locks for its duration (the block RAMs are busy). See
        # docs/guide/timing_model/insertion.md.
        cycles = float(self.timing.predict({"nw": nw}))
        yield self.timeout(max(0.0, cycles) * self.clk.period)
        yield from self.p_blk.release_read()
        yield from self.x_blk.release_read()
        yield from self.y_blk.commit_write(yblock)
        self.job_start_cyc.append(t0 / self.clk.period)
        self.job_end_cyc.append(self.now / self.clk.period)
        self.job_span_cyc.append((self.now - t0) / self.clk.period)
        self.fire_log.append((t0 / self.clk.period, self.now / self.clk.period))


@dataclass
class IlStore(FreeRunComp):
    """Stage 5 (SOB->stream): token + read-lock y_blk -> ywords stream -> forward token."""

    cpp_kernel_name: ClassVar[str | None] = "il_store"
    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    n: HwParam[int] = DEFAULT_N
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        self.lw = w // 32
        self.nw = _nwords(int(self.n), self.lw)
        self.cmd_in = StreamIFSlave(name=f"{self.name}_cmd_in", sim=self.sim, bitwidth=w,
                                    has_tlast=False)
        word_block = _make_word_block(w, self.nw)
        self.y_blk = SobIFSlave(name=f"{self.name}_y_blk", sim=self.sim, element_type=word_block)
        self.cmd_out = StreamIFMaster(name=f"{self.name}_cmd_out", sim=self.sim, bitwidth=w,
                                      has_tlast=False)
        self.ywords = StreamIFMaster(name=f"{self.name}_ywords", sim=self.sim, bitwidth=w,
                                     has_tlast=False)
        for ep in (self.cmd_in, self.y_blk, self.cmd_out, self.ywords):
            self.add_endpoint(ep)
        self.fire_log: list[tuple[float, float]] = []

    def kernel_task(self) -> KernelTask:
        return KernelTask("il_store_task", "il_store_task.h",
                          ("cmd_in", "y_blk", "cmd_out", "ywords"),
                          template_args=(int(self.mem_dwidth), self.nw))

    def run_iter(self) -> ProcessGen[None]:
        cmd = yield from self.cmd_in.get(_TOKEN)
        t0 = self.now
        yield from self.cmd_out.write(cmd)
        yblock = yield from self.y_blk.acquire_read()
        yield from self.ywords.write(np.asarray(yblock))
        yield from self.y_blk.release_read()
        self.fire_log.append((t0 / self.clk.period, self.now / self.clk.period))


@dataclass
class IlMemW(FreeRunComp):
    """Stage 6 (m_axi write owner, gmem1): token + ywords -> write Y -> emit token on s_done (after
    the write burst — the commit-timed completion)."""

    cpp_kernel_name: ClassVar[str | None] = "il_mem_w"
    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    n: HwParam[int] = DEFAULT_N
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        self.lw = w // 32
        self.nw = _nwords(int(self.n), self.lw)
        self.cmd_in = StreamIFSlave(name=f"{self.name}_cmd_in", sim=self.sim, bitwidth=w,
                                    has_tlast=False)
        self.ywords = StreamIFSlave(name=f"{self.name}_ywords", sim=self.sim, bitwidth=w,
                                    has_tlast=False)
        # The sole m_axi WRITE owner (see IlMemR.m_mem).
        self.m_mem = MMIFWriteMaster(name=f"{self.name}_m_mem", sim=self.sim, bitwidth=w)
        self.s_done = StreamIFMaster(name=f"{self.name}_s_done", sim=self.sim, bitwidth=w,
                                     has_tlast=False)
        for ep in (self.cmd_in, self.ywords, self.m_mem, self.s_done):
            self.add_endpoint(ep)
        self._wt, self._bw = _word_t(w), w
        self.fire_log: list[tuple[float, float]] = []

    def kernel_task(self) -> KernelTask:
        return KernelTask("il_mem_w_task", "il_mem_w_task.h",
                          ("cmd_in", "ywords", "m_mem", "s_done"),
                          template_args=(int(self.mem_dwidth), self.nw))

    def run_iter(self) -> ProcessGen[None]:
        nw = self.nw
        cmd = yield from self.cmd_in.get(_TOKEN)
        t0 = self.now
        yw = int(cmd.y_off)
        words = yield from self.ywords.get(nwords_max=nw)
        region = self.m_mem.region(0, self._wt, word_bw=self._bw)
        yield from region.write_slice_pipelined(
            yw, np.asarray(words), t_out_start=self.now, element_type=self._wt)
        yield from self.s_done.write(cmd)        # commit-timed completion token
        self.fire_log.append((t0 / self.clk.period, self.now / self.clk.period))


@dataclass
class InterleaverCanon(FreeRunComp):
    """The canonical six-stage interleaver with a forwarded per-job token: ``cmd_rx -> il_mem_r ->
    il_load -> il_compute -> il_store -> il_mem_w``.  Five Cmd StreamEdges (the token, one per hop) +
    three data StreamEdges (pwords, xwords, ywords) + three SobEdges (p_blk, x_blk, y_blk)."""

    cpp_kernel_name: ClassVar[str | None] = "interleaver_canon"
    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    n: HwParam[int] = DEFAULT_N
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    #: Optional directory with the custom compute stage's fitted loop model (``params.json``); ``None``
    #: leaves ``IlCompute`` on its seed. The framework mem stages take their timing from the platform.
    compute_calib_dir: "str | None" = None

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        n = int(self.n)
        self.lw = w // 32
        self.nw = _nwords(n, self.lw)

        self.rx = CmdRx(name=f"{self.name}_rx", sim=self.sim, mem_dwidth=w, clk=self.clk)
        self.memr = IlMemR(name=f"{self.name}_memr", sim=self.sim, mem_dwidth=w, n=n, clk=self.clk)
        self.load = IlLoad(name=f"{self.name}_load", sim=self.sim, mem_dwidth=w, n=n, clk=self.clk)
        self.compute = IlCompute(name=f"{self.name}_compute", sim=self.sim, mem_dwidth=w, n=n,
                                 clk=self.clk, calib_dir=self.compute_calib_dir)
        self.store = IlStore(name=f"{self.name}_store", sim=self.sim, mem_dwidth=w, n=n, clk=self.clk)
        self.memw = IlMemW(name=f"{self.name}_memw", sim=self.sim, mem_dwidth=w, n=n, clk=self.clk)
        stages = [self.rx, self.memr, self.load, self.compute, self.store, self.memw]
        for c in stages:
            self.add_comp(c)
        self.gather = self.compute          # the completion-timeline probe (job_end_cyc)

        def _sif(name, master, slave):
            iface = StreamIF(name=f"{self.name}_{name}_if", sim=self.sim, clk=self.clk, bitwidth=w)
            iface.bind("master", master)
            iface.bind("slave", slave)
            self.add_if(iface)

        def _sobif(name, master, slave):
            word_block = _make_word_block(w, self.nw)
            iface = StreamOfBlocksIF(name=f"{self.name}_{name}_if", sim=self.sim, clk=self.clk,
                                     element_type=word_block)
            iface.bind("master", master)
            iface.bind("slave", slave)
            self.add_if(iface)

        # five Cmd token hops
        _sif("cmd0", self.rx.cmd_out, self.memr.cmd_in)
        _sif("cmd1", self.memr.cmd_out, self.load.cmd_in)
        _sif("cmd2", self.load.cmd_out, self.compute.cmd_in)
        _sif("cmd3", self.compute.cmd_out, self.store.cmd_in)
        _sif("cmd4", self.store.cmd_out, self.memw.cmd_in)
        # data edges
        _sif("pwords", self.memr.pwords, self.load.pwords)
        _sif("xwords", self.memr.xwords, self.load.xwords)
        _sif("ywords", self.store.ywords, self.memw.ywords)
        # block edges
        _sobif("p_blk", self.load.p_blk, self.compute.p_blk)
        _sobif("x_blk", self.load.x_blk, self.compute.x_blk)
        _sobif("y_blk", self.compute.y_blk, self.store.y_blk)

        # The eleven internal edges ARE the _sif/_sobif calls above: each add_if records both
        # endpoints and (by its type) how the edge lowers -- a StreamIF to an hls::stream, a
        # StreamOfBlocksIF to a stream_of_blocks sized by its element_type.  Derived, not restated.
        #
        # Boundary port NAMES only -- endpoints and order come from the graph, direction from the
        # endpoint type, gmem bundle by policy in this order.
        self.boundary = ["s_cmd", "m_in", "m_out", "s_done"]
        self.cmd_headers = tuple(dict.fromkeys(c.resolved_include_filename() for c in SCHEMA_CLASSES))
        self.extra_includes = ("hls_streamofblocks.h",)

        self.s_cmd = self.rx.s_cmd
        self.m_in = self.memr.m_mem
        self.m_out = self.memw.m_mem
        self.s_done = self.memw.s_done


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def gen_headers(config: BuildConfig, mem_dwidth: int = DEFAULT_MEM_DW) -> None:
    """Generate the command headers + memmgr + streamutils + the fixed task bodies + the block
    element type's array-utils header (elem_read<MEM_DW>) into ``include/``."""
    inner = BuildDag()
    inner.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    inner.add(MemMgrStep(output_dir=INCLUDE_DIR))
    inner.add(MemStreamStep(output_dir=INCLUDE_DIR))
    for cls in SCHEMA_CLASSES:
        inner.add(DataSchemaStep(cls, word_bw_supported=WORD_BW_SUPPORTED, include_dir=INCLUDE_DIR))
    results = inner.run(config, force=True)
    failed = [n for n, r in results.items() if not r.success]
    if failed:
        raise RuntimeError(f"gen-include failed: {failed}")
    # il_elem_array_utils.h — elem_read<MEM_DW> for the word-granular Gather.
    gen_array_utils(IlElem, [int(mem_dwidth)], cfg=config, streamutils_dir=INCLUDE_DIR)


def _emit_top(comp, out_dir: Path, mem_dwidth: int) -> Path:
    """Render *comp*'s composite top .cpp + csynth .tcl into ``out_dir/gen`` + ``out_dir``."""
    spec = composite_top_spec(comp, width=mem_dwidth)
    gen = out_dir / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    cpp = gen / f"{spec.top_name}.cpp"
    cpp.write_text(render_top(spec), encoding="utf-8")
    (out_dir / f"{spec.top_name}.tcl").write_text(render_tcl(spec.top_name), encoding="utf-8")
    ports_h = out_dir / "xsi" / f"{spec.top_name}_ports.h"
    ports_h.parent.mkdir(parents=True, exist_ok=True)
    ports_h.write_text(render_ports_h(spec), encoding="utf-8")
    print(f"generated {cpp.relative_to(out_dir)} + {spec.top_name}.tcl")
    return cpp


def generate_canon(out_dir: Path = HERE, mem_dwidth: int = DEFAULT_MEM_DW, n: int = DEFAULT_N) -> Path:
    """Generate headers + the canonical six-stage :class:`InterleaverCanon` top .cpp + .tcl."""
    from waveflow.build.elaborate import elaborate

    config = BuildConfig(root_dir=out_dir, params={})
    gen_headers(config, mem_dwidth=mem_dwidth)
    comp = elaborate(
        InterleaverCanon, {"mem_dwidth": mem_dwidth, "n": n}, name="interleaver_canon"
    )
    return _emit_top(comp, out_dir, mem_dwidth)


if __name__ == "__main__":
    generate_canon()
