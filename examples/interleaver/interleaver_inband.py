"""interleaver_inband.py — the interleaver rebuilt on the framework MemRStream / MemWStream.

The gather `Y[i] = X[P[i]]` composed on the **framework** in-band mem-streams — the same
`MemRStream` / `MemWStream(inband)` that `mem_copy` uses — so the interleaver inherits their shipped,
XSI-verified timing and the design owns only its **custom compute**.

Two descriptor types, following mem_copy's convention (a plain boundary command + framed internal
descriptors), so every inter-component stream is **framed** and only the host boundary is plain:

* **`InterleaverCmd`** — the host command on the boundary `s_cmd` (plain word stream): `{p_off, x_off,
  y_off, n}`.
* **`IlDesc`** — the framed internal descriptor forwarded through the pipeline: `{n, y_off}`. The
  length `n` rides it, so the RTL is **scenario-independent** (variable length) rather than baking a
  size.

Flow (all internal edges framed):

* **`cmd_rx`** reads one `InterleaverCmd` and frames the reader's command stream as **two reads**:
  `[MemRCmd(p_off, nw, fwd=1) | IlDesc | MemRCmd(x_off, nw, fwd=0)]`, `nw = ceil(n/LW)`. The `fwd=1`
  relays the `IlDesc` as a header ahead of the P data.
* **`MemRStream(inband)`** fires twice → `m_out = [IlDesc | P | X]`.
* **`il_load`** reads the descriptor, then **deserializes** P → `p_blk` and X → `x_blk` — 32-bit
  **element** blocks (stream-of-blocks) via `read_framed_stream_lane` — and forwards `IlDesc`.
* **`il_compute`** — the custom gather, laid bare on the typed blocks: `y_blk[i] = x_blk[p_blk[i]]`
  (no lane math), carrying its own loop timing model.
* **`il_store`** **serializes** `y_blk` back to words (`write_framed_stream_lane`) and frames the writer's
  stream `[MemWCmd(y_off, nw, fwd=1) | IlDesc | Y]`.
* **`MemWStream(inband, emit_done)`** writes Y and echoes `IlDesc` on `s_done` — the commit-timed done.

`cmd_rx` and `il_store` are the only schema-aware framers; the mem-streams relay opaquely. pysim-verified
against the `Y=X[P]` golden; codegen + XSI is the toolchain follow-up (see the three
``il_*_inband_task.h`` bodies). Run via ``interleaver_sim.run_interleaver(comp_class=InterleaverInband)``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataArray, DataList, IntField
from waveflow.hw.hw_component import HwParam
from waveflow.hw.hw_freerun import FreeRunComp
from waveflow.hw.interface import (
    SobIFMaster,
    SobIFSlave,
    StreamIF,
    StreamIFMaster,
    StreamIFSlave,
    StreamOfBlocksIF,
)
from waveflow.hw.mem_stream import KernelTask, MemRCmd, MemRStream, MemWCmd, MemWStream
from waveflow.simulation.simobj import ProcessGen

from examples.interleaver.interleaver import (
    DEFAULT_MEM_DW,
    DEFAULT_N,
    IL_COMPUTE_II_SEED,
    IL_COMPUTE_LATENCY_SEED,
    InterleaverCmd,
    _nwords,
)

Word32 = IntField.specialize(bitwidth=32, signed=False)


class IlDesc(DataList):
    """The framed internal descriptor forwarded through the pipeline — the length ``n`` (so the RTL is
    scenario-independent) and the output offset ``y_off`` the writer needs. Relayed opaquely by the
    mem-streams and echoed on ``s_done``; read/written framed by the custom stages."""
    include_filename: ClassVar[str | None] = "il_desc.h"
    elements = {
        "n":     {"schema": Word32, "description": "number of elements this job gathers"},
        "y_off": {"schema": Word32, "description": "Y (output) buffer word offset"},
    }


def _nw_of(n: int, lw: int) -> int:
    return (n + lw - 1) // lw


def _make_elem_block(n: int) -> type:
    """A typed **element** block: ``DataArray`` of 32-bit elements (up to ``n``) → a 32-bit-wide block RAM
    the gather indexes DIRECTLY (``y[i] = x[p[i]]``).  Replaces the packed WORD block (``ap_uint<MEM_DW>``)
    the compute used to unpack/pack by hand — the (de)serialization now lives in ``il_load`` / ``il_store``
    via the generated ``read_framed_stream_lane`` / ``write_framed_stream_lane`` (see
    guide/vectorization/hls/arrayutils.md).  Same total bits as ``_make_word_block`` (``n``·32 = ``nw``·MEM_DW)."""
    return DataArray.specialize(element_type=Word32, max_shape=(int(n),), member_name="elems")


def _words_to_elems(words, mem_dwidth: int):
    """Deserialize MEM_DW words → 32-bit elements (LSB-first) — the pysim twin of the RTL's
    ``read_framed_stream_lane`` filling an element block."""
    word_dt = np.dtype(f"<u{int(mem_dwidth) // 8}")
    return np.asarray(words, dtype=word_dt).view(np.uint32)


def _elems_to_words(elems, mem_dwidth: int):
    """Serialize 32-bit elements → MEM_DW words (LSB-first) — the pysim twin of ``write_framed_stream_lane``.
    Pads the final word if the element count isn't a multiple of LW."""
    lw = int(mem_dwidth) // 32
    e = np.asarray(elems, dtype=np.uint32)
    if e.size % lw:
        e = np.concatenate([e, np.zeros(lw - e.size % lw, dtype=np.uint32)])
    return e.view(np.dtype(f"<u{int(mem_dwidth) // 8}"))


@dataclass
class CmdRxInband(FreeRunComp):
    """Framer (mem_copy's ``Sequencer`` role): read one ``InterleaverCmd`` and frame the reader's
    command stream as **two reads** to the transactional ``MemRStream`` (the arbiter model — a consumer
    issues N reads per job): P (descriptor relayed as a header, ``fwd=1``) then X (``fwd=0``). The
    ``fwd=0`` read needs the reader's ``nfwd>0`` relay guard. See :meth:`run_iter`."""

    cpp_kernel_name: ClassVar[str | None] = "il_cmd_rx"
    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    n: HwParam[int] = DEFAULT_N
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        self.lw = w // 32
        self.nw = _nwords(int(self.n), self.lw)
        self.s_cmd = StreamIFSlave(name=f"{self.name}_s_cmd", sim=self.sim, bitwidth=w,
                                   has_tlast=False)   # plain host boundary
        self.cmd_out = StreamIFMaster(name=f"{self.name}_cmd_out", sim=self.sim, bitwidth=w,
                                      has_tlast=True)   # framed → MemRStream
        for ep in (self.s_cmd, self.cmd_out):
            self.add_endpoint(ep)
        self.fire_log: list[tuple[float, float]] = []

    def kernel_task(self) -> KernelTask:
        return KernelTask("il_cmd_rx_framed_task", "il_cmd_rx_framed_task.h", ("s_cmd", "cmd_out"),
                          template_args=(int(self.mem_dwidth),))

    def run_iter(self) -> ProcessGen[None]:
        w = int(self.mem_dwidth)
        cmd = yield from self.s_cmd.get(InterleaverCmd)
        t0 = self.now
        n = int(cmd.n)
        nw = _nw_of(n, self.lw)                     # runtime word count
        desc = IlDesc(n=n, y_off=int(cmd.y_off))
        # TWO reads to the transactional MemRStream (arbiter model): P (relaying the descriptor as a
        # header, fwd=1) then X (relay nothing, fwd=0).  P first so il_load fills p_blk before x_blk
        # (il_compute read-locks p_blk first).  The fwd=0 second read needs the reader's `nfwd>0` relay
        # guard — without it the RTL reader mis-relays a phantom word and deadlocks (the pysim for-loop
        # relay was always correct, which is why this pysim passed while the RTL wedged).
        memr_p = MemRCmd(addr=int(cmd.p_off), len=nw, fwd_bursts=1)
        memr_x = MemRCmd(addr=int(cmd.x_off), len=nw, fwd_bursts=0)
        yield from self.cmd_out.write(np.asarray(memr_p.serialize(word_bw=w), dtype=np.uint64))
        yield from self.cmd_out.write(np.asarray(desc.serialize(word_bw=w), dtype=np.uint64))
        yield from self.cmd_out.write(np.asarray(memr_x.serialize(word_bw=w), dtype=np.uint64))
        self.fire_log.append((t0 / self.clk.period, self.now / self.clk.period))


@dataclass
class IlLoadInband(FreeRunComp):
    """Reads the framed ``[IlDesc | P | X]`` off the reader, fills ``p_blk`` / ``x_blk`` (SOB, p_blk
    FIRST — see :class:`CmdRxInband`), and forwards the descriptor to the compute."""

    cpp_kernel_name: ClassVar[str | None] = "il_load"
    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    n: HwParam[int] = DEFAULT_N
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        self.lw = w // 32
        self.nw = _nwords(int(self.n), self.lw)
        self.s_in = StreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=w,
                                  has_tlast=True)   # framed ← MemRStream
        self.desc_out = StreamIFMaster(name=f"{self.name}_desc_out", sim=self.sim, bitwidth=w,
                                       has_tlast=True)   # framed → compute
        elem_block = _make_elem_block(int(self.n))
        self.p_blk = SobIFMaster(name=f"{self.name}_p_blk", sim=self.sim, element_type=elem_block)
        self.x_blk = SobIFMaster(name=f"{self.name}_x_blk", sim=self.sim, element_type=elem_block)
        for ep in (self.s_in, self.desc_out, self.p_blk, self.x_blk):
            self.add_endpoint(ep)
        self.fire_log: list[tuple[float, float]] = []

    def kernel_task(self) -> KernelTask:
        return KernelTask("il_load_inband_task", "il_load_inband_task.h",
                          ("s_in", "desc_out", "p_blk", "x_blk"),
                          template_args=(int(self.mem_dwidth), int(self.n)))

    def run_iter(self) -> ProcessGen[None]:
        w = int(self.mem_dwidth)
        desc = yield from self.s_in.get(IlDesc)          # descriptor (header)
        t0 = self.now
        n = int(desc.n)
        nw = _nw_of(n, self.lw)
        yield from self.desc_out.write(np.asarray(desc.serialize(word_bw=w), dtype=np.uint64))
        # Two bursts on rdata (the reader's two firings): P then X, nw words each.  DESERIALIZE each burst
        # into n 32-bit elements (read_framed_stream_lane's job) and fill the element blocks.  p_blk first
        # so it is released before x_blk, matching il_compute's read-lock order.
        pblock = yield from self.p_blk.acquire_write()
        pw = yield from self.s_in.get(nwords_max=nw)
        pblock[:n] = _words_to_elems(pw, w)[:n]
        yield from self.p_blk.commit_write(pblock)
        xblock = yield from self.x_blk.acquire_write()
        xw = yield from self.s_in.get(nwords_max=nw)
        xblock[:n] = _words_to_elems(xw, w)[:n]
        yield from self.x_blk.commit_write(xblock)
        self.fire_log.append((t0 / self.clk.period, self.now / self.clk.period))


@dataclass
class IlComputeInband(FreeRunComp):
    """The custom gather (SOB→SOB), variable length: read ``IlDesc`` → ``nw``, gather ``nw`` words, and
    forward the descriptor. Carries its own loop timing model (the thing the design fits)."""

    cpp_kernel_name: ClassVar[str | None] = "il_compute"
    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    n: HwParam[int] = DEFAULT_N
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    calib_dir: "str | None" = None

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        self.lw = w // 32
        self.nw = _nwords(int(self.n), self.lw)
        self.desc_in = StreamIFSlave(name=f"{self.name}_desc_in", sim=self.sim, bitwidth=w,
                                     has_tlast=True)
        elem_block = _make_elem_block(int(self.n))
        self.p_blk = SobIFSlave(name=f"{self.name}_p_blk", sim=self.sim, element_type=elem_block)
        self.x_blk = SobIFSlave(name=f"{self.name}_x_blk", sim=self.sim, element_type=elem_block)
        self.desc_out = StreamIFMaster(name=f"{self.name}_desc_out", sim=self.sim, bitwidth=w,
                                       has_tlast=True)
        self.y_blk = SobIFMaster(name=f"{self.name}_y_blk", sim=self.sim, element_type=elem_block)
        for ep in (self.desc_in, self.p_blk, self.x_blk, self.desc_out, self.y_blk):
            self.add_endpoint(ep)
        self.timing = self._build_timing_model()
        self.job_start_cyc: list[float] = []
        self.job_end_cyc: list[float] = []
        self.job_span_cyc: list[float] = []
        self.fire_log: list[tuple[float, float]] = []

    def _build_timing_model(self):
        from waveflow.calib.calib import LinCalibModel

        seed = {"n": IL_COMPUTE_II_SEED, "intercept": IL_COMPUTE_LATENCY_SEED - IL_COMPUTE_II_SEED}
        path = None if self.calib_dir is None else Path(self.calib_dir) / "params.json"
        model = LinCalibModel(basis=["n"], target="cycles", fit_intercept=True,
                              coeff_names=["n"], seed=seed, path=path)
        model.load_or_default()
        return model

    def kernel_task(self) -> KernelTask:
        return KernelTask("il_compute_inband_task", "il_compute_inband_task.h",
                          ("desc_in", "p_blk", "x_blk", "desc_out", "y_blk"),
                          template_args=(int(self.mem_dwidth), int(self.n)))

    def run_iter(self) -> ProcessGen[None]:
        w = int(self.mem_dwidth)
        desc = yield from self.desc_in.get(IlDesc)
        n = int(desc.n)
        yield from self.desc_out.write(np.asarray(desc.serialize(word_bw=w), dtype=np.uint64))
        pblock = yield from self.p_blk.acquire_read()
        xblock = yield from self.x_blk.acquire_read()
        yblock = yield from self.y_blk.acquire_write()
        t0 = self.now
        yblock.val[:n] = xblock.val[pblock.val[:n]]     # the gather, vectorized: Y[i] = X[P[i]]
        cycles = float(self.timing.predict({"n": n}))
        yield self.timeout(max(0.0, cycles) * self.clk.period)
        yield from self.p_blk.release_read()
        yield from self.x_blk.release_read()
        yield from self.y_blk.commit_write(yblock)
        self.job_start_cyc.append(t0 / self.clk.period)
        self.job_end_cyc.append(self.now / self.clk.period)
        self.job_span_cyc.append((self.now - t0) / self.clk.period)
        self.fire_log.append((t0 / self.clk.period, self.now / self.clk.period))


@dataclass
class IlStoreInband(FreeRunComp):
    """Reads ``y_blk`` and frames the writer's stream ``[MemWCmd | IlDesc | Y]``."""

    cpp_kernel_name: ClassVar[str | None] = "il_store"
    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    n: HwParam[int] = DEFAULT_N
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        self.lw = w // 32
        self.nw = _nwords(int(self.n), self.lw)
        self.desc_in = StreamIFSlave(name=f"{self.name}_desc_in", sim=self.sim, bitwidth=w,
                                     has_tlast=True)
        elem_block = _make_elem_block(int(self.n))
        self.y_blk = SobIFSlave(name=f"{self.name}_y_blk", sim=self.sim, element_type=elem_block)
        self.cmd_out = StreamIFMaster(name=f"{self.name}_cmd_out", sim=self.sim, bitwidth=w,
                                      has_tlast=True)   # framed → MemWStream
        for ep in (self.desc_in, self.y_blk, self.cmd_out):
            self.add_endpoint(ep)
        self.fire_log: list[tuple[float, float]] = []

    def kernel_task(self) -> KernelTask:
        return KernelTask("il_store_inband_task", "il_store_inband_task.h",
                          ("desc_in", "y_blk", "cmd_out"),
                          template_args=(int(self.mem_dwidth), int(self.n)))

    def run_iter(self) -> ProcessGen[None]:
        w = int(self.mem_dwidth)
        desc = yield from self.desc_in.get(IlDesc)
        t0 = self.now
        n = int(desc.n)
        nw = _nw_of(n, self.lw)
        yblock = yield from self.y_blk.acquire_read()
        # Frame the writer's stream: descriptor (addr=y_off, nw words), echo the IlDesc (emitted on
        # s_done after the store), then the Y data — SERIALIZED from the n gathered elements to nw words
        # (write_framed_stream_lane's job).
        memw = MemWCmd(addr=int(desc.y_off), len=nw, fwd_bursts=1)
        yield from self.cmd_out.write(np.asarray(memw.serialize(word_bw=w), dtype=np.uint64))
        yield from self.cmd_out.write(np.asarray(desc.serialize(word_bw=w), dtype=np.uint64))
        yield from self.cmd_out.write(np.asarray(_elems_to_words(yblock[:n], w), dtype=np.uint64))
        yield from self.y_blk.release_read()
        self.fire_log.append((t0 / self.clk.period, self.now / self.clk.period))


@dataclass
class InterleaverInband(FreeRunComp):
    """The interleaver composed on the framework in-band mem-streams: ``cmd_rx → MemRStream →
    il_load → il_compute → il_store → MemWStream``.  Every internal edge is framed (the mem-stream
    edges and the descriptor edges); the read/write adaptors are framework."""

    cpp_kernel_name: ClassVar[str | None] = "interleaver_inband"
    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    n: HwParam[int] = DEFAULT_N
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))
    compute_calib_dir: "str | None" = None

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        n = int(self.n)
        self.lw = w // 32
        self.nw = _nwords(n, self.lw)

        self.rx = CmdRxInband(name=f"{self.name}_rx", sim=self.sim, mem_dwidth=w, n=n, clk=self.clk)
        self.rstream = MemRStream(name=f"{self.name}_memr", sim=self.sim, mem_dwidth=w, inband=True,
                                  clk=self.clk)
        self.load = IlLoadInband(name=f"{self.name}_load", sim=self.sim, mem_dwidth=w, n=n,
                                 clk=self.clk)
        self.compute = IlComputeInband(name=f"{self.name}_compute", sim=self.sim, mem_dwidth=w, n=n,
                                       clk=self.clk, calib_dir=self.compute_calib_dir)
        self.store = IlStoreInband(name=f"{self.name}_store", sim=self.sim, mem_dwidth=w, n=n,
                                   clk=self.clk)
        self.wstream = MemWStream(name=f"{self.name}_memw", sim=self.sim, mem_dwidth=w, inband=True,
                                  emit_done=True, clk=self.clk)
        for c in (self.rx, self.rstream, self.load, self.compute, self.store, self.wstream):
            self.add_comp(c)
        self.gather = self.compute          # the completion-timeline probe (job_end_cyc)

        def _sif(name, master, slave, depth=None):
            iface = StreamIF(name=f"{self.name}_{name}_if", sim=self.sim, clk=self.clk, bitwidth=w,
                             framed=True, **({} if depth is None else {"depth": depth}))
            iface.bind("master", master)
            iface.bind("slave", slave)
            self.add_if(iface)

        def _sobif(name, master, slave):
            elem_block = _make_elem_block(int(self.n))
            iface = StreamOfBlocksIF(name=f"{self.name}_{name}_if", sim=self.sim, clk=self.clk,
                                     element_type=elem_block)
            iface.bind("master", master)
            iface.bind("slave", slave)
            self.add_if(iface)

        _sif("cmd_rd", self.rx.cmd_out, self.rstream.s_cmd)     # [MemRCmd|IlDesc|MemRCmd]
        _sif("rdata", self.rstream.m_out, self.load.s_in)       # [IlDesc | P | X]
        _sif("desc_lc", self.load.desc_out, self.compute.desc_in)   # IlDesc
        _sif("desc_cs", self.compute.desc_out, self.store.desc_in)  # IlDesc
        _sif("wdata", self.store.cmd_out, self.wstream.s_in)    # [MemWCmd|IlDesc|Y]
        _sobif("p_blk", self.load.p_blk, self.compute.p_blk)
        _sobif("x_blk", self.load.x_blk, self.compute.x_blk)
        _sobif("y_blk", self.compute.y_blk, self.store.y_blk)

        self.boundary = ["s_cmd", "m_in", "m_out", "s_done"]
        self.s_cmd = self.rx.s_cmd
        self.m_in = self.rstream.m_mem
        self.m_out = self.wstream.m_mem
        self.s_done = self.wstream.s_done


# ---------------------------------------------------------------------------
# Codegen driver — the in-band DUT (headers + composite top + csynth tcl)
# ---------------------------------------------------------------------------

from waveflow.build.build import BuildConfig, BuildDag  # noqa: E402
from waveflow.build.composite_gen import (  # noqa: E402
    GEN_DIR,
    INCLUDE_DIR,
    composite_top_spec,
    render_ports_h,
    render_tcl,
    render_top,
)
from waveflow.build.streamutils import MemMgrStep, MemStreamStep, StreamUtilsStep  # noqa: E402
from waveflow.hw.arrayutils import gen_array_utils  # noqa: E402
from waveflow.hw.dataschema import DataSchemaStep  # noqa: E402
from waveflow.hw.mem_stream import WORD_BW_SUPPORTED  # noqa: E402

from examples.interleaver.interleaver import IlElem  # noqa: E402

_HERE = Path(__file__).resolve().parent

#: The command structs the generated top #includes.  InterleaverCmd is the PLAIN boundary command;
#: IlDesc / MemRCmd / MemWCmd ride the framed edges (so they emit framed_word read/write methods).
SCHEMA_CLASSES = [InterleaverCmd, IlDesc, MemRCmd, MemWCmd]
FRAMED_SCHEMAS = frozenset({IlDesc, MemRCmd, MemWCmd})


def gen_headers(config: BuildConfig, mem_dwidth: int = DEFAULT_MEM_DW) -> None:
    """Generate the command headers + memmgr + streamutils + the fixed framed task bodies + the block
    element type's array-utils header (elem_read<MEM_DW>) into ``include/``."""
    inner = BuildDag()
    inner.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    inner.add(MemMgrStep(output_dir=INCLUDE_DIR))
    inner.add(MemStreamStep(output_dir=INCLUDE_DIR))
    for cls in SCHEMA_CLASSES:
        inner.add(DataSchemaStep(cls, word_bw_supported=WORD_BW_SUPPORTED, include_dir=INCLUDE_DIR,
                                 framed=(cls in FRAMED_SCHEMAS)))
    results = inner.run(config, force=True)
    failed = [n for n, r in results.items() if not r.success]
    if failed:
        raise RuntimeError(f"gen-include failed: {failed}")
    gen_array_utils(IlElem, [int(mem_dwidth)], cfg=config, streamutils_dir=INCLUDE_DIR)


def generate_inband(out_dir: Path = _HERE, mem_dwidth: int = DEFAULT_MEM_DW,
                    n: int = DEFAULT_N) -> Path:
    """Generate the in-band interleaver DUT: headers + the composite top .cpp + its csynth .tcl + the
    port map.  Runnable without the toolchain up to the csynth call."""
    from waveflow.build.elaborate import elaborate

    out_dir = Path(out_dir)
    config = BuildConfig(root_dir=out_dir, params={})
    gen_headers(config, mem_dwidth=mem_dwidth)
    comp = elaborate(InterleaverInband, {"mem_dwidth": mem_dwidth, "n": n}, name="interleaver_inband")
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


# ---------------------------------------------------------------------------
# XSI testbench codegen — the BFM harness derived from the InterleaverInbandTB graph
# ---------------------------------------------------------------------------

from waveflow.build.composite_gen import (  # noqa: E402
    render_tb_harness,
    render_tb_main,
    render_vectors_h,
    tb_top_spec,
)

_TOP = "interleaver_inband"


def _done_words(width: int) -> int:
    """Words per ``s_done`` completion — one echoed :class:`IlDesc`.  At w=64 this is 1."""
    return IlDesc.nwords_per_inst(width)


def make_xsi_tb(width: int = DEFAULT_MEM_DW, sizes=(256,), n_cycles: "int | None" = None):
    """The :class:`~examples.interleaver.interleaver_inband_sim.InterleaverInbandTB` the XSI testbench
    is generated from (lazy import — the sim module imports this one)."""
    from waveflow.simulation.simulation import Simulation
    from examples.interleaver.interleaver_inband_sim import InterleaverInbandTB

    kw = {} if n_cycles is None else {"n_cycles": int(n_cycles)}
    return InterleaverInbandTB(name="xsi_tb", sim=Simulation(), sizes=tuple(sizes),
                               mem_dwidth=width, **kw)


def render_xsi_vectors(width: int = DEFAULT_MEM_DW, sizes=(256,)) -> str:
    """Render ``interleaver_inband_vectors.h`` from the testbench graph — the arena size + command
    count the harness needs.  The per-job offsets ride the ``vectors/s_cmd`` bundle."""
    tb = make_xsi_tb(width, sizes)
    return render_vectors_h(
        f"{_TOP}_vectors",
        scalars={
            "MEM_DW": width,
            "MEM_NW": int(tb.mem.nwords_tot),
            "NUM_CMDS": len(tb._sizes),
            "DONE_WORDS": _done_words(width),
        },
        note=("Derived from the InterleaverInbandTB graph (examples/interleaver/interleaver_inband_sim.py)\n"
              "-- the same class the pysim golden runs.  The command words + P/X arena + golden ride the\n"
              "burst bundles under xsi/vectors/, written by write_xsi_bundles."),
    )


def gen_xsi_vectors(out_dir: Path = _HERE, width: int = DEFAULT_MEM_DW, sizes=(256,)) -> Path:
    path = Path(out_dir) / "xsi" / f"{_TOP}_vectors.h"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_xsi_vectors(width, sizes), encoding="utf-8")
    return path


def write_xsi_bundles(xsi_dir: Path, width: int = DEFAULT_MEM_DW, sizes=(256,)) -> None:
    """Write the XSI input + golden bundles into ``<xsi_dir>/vectors/`` (s_cmd, mem_in, golden) — the
    single scenario writer both backends share."""
    from examples.interleaver.interleaver_inband_sim import InterleaverInbandSim

    InterleaverInbandSim(sizes=tuple(sizes), mem_dwidth=width).write_scenario(Path(xsi_dir))


def check_xsi_outputs(xsi_dir: Path, width: int = DEFAULT_MEM_DW, sizes=(256,)) -> None:
    """Check the XSI run from the dumped bundles: every Y region equals the golden, one done per job."""
    from waveflow.utils.burst_io import read_burst_bundle

    vdir = Path(xsi_dir) / "vectors"
    lw = width // 32
    out = read_burst_bundle(vdir / "out")[0]
    golden = read_burst_bundle(vdir / "golden")[0]
    cur = 0
    for j, n in enumerate(sizes):
        nw = (n + lw - 1) // lw
        y = cur + 2 * nw
        cur += 3 * nw
        if not np.array_equal(out[y:y + nw], golden[y:y + nw]):
            bad = int(np.argmax(out[y:y + nw] != golden[y:y + nw]))
            raise AssertionError(f"interleaver_inband job {j} word {bad}: "
                                 f"0x{int(out[y + bad]):016x} != golden 0x{int(golden[y + bad]):016x}")
    s_done = read_burst_bundle(vdir / "s_done")[0]
    dw = _done_words(width)
    assert len(s_done) == len(sizes) * dw, (
        f"interleaver_inband: s_done has {len(s_done)} words, expected {len(sizes)}*{dw}")


def generate_tb(out_dir: Path = _HERE, width: int = DEFAULT_MEM_DW, sizes=(256,),
                n_cycles: "int | None" = None) -> dict:
    """Generate the XSI testbench: the scenario constants + the BFM harness + the two-line main, all
    derived from the InterleaverInbandTB graph (the harness #includes the DUT's ports.h)."""
    vec_h = gen_xsi_vectors(out_dir, width=width, sizes=sizes)
    tb = make_xsi_tb(width, sizes=sizes, n_cycles=n_cycles)
    tb_spec = tb_top_spec(tb)
    harness_h = Path(out_dir) / "xsi" / f"{_TOP}_tb_harness.h"
    harness_h.write_text(render_tb_harness(tb_spec), encoding="utf-8")
    main_cpp = Path(out_dir) / "xsi" / f"{_TOP}_bfm_tb.cpp"
    main_cpp.write_text(render_tb_main(tb_spec, tb.n_cycles), encoding="utf-8")
    print(f"generated TB xsi/{vec_h.name} + xsi/{harness_h.name} + xsi/{main_cpp.name}")
    return {"tb_harness": harness_h, "tb_main": main_cpp}
