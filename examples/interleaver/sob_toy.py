"""sob_toy.py — the Phase-3 SOBIF de-risk: a generated pure-AXIS ``Fill ->SOBIF-> Gather`` top.

The generated analogue of the hand-written sandbox ``interleaver_sob_task.cpp`` (pure-AXIS, NO m_axi):
two free-running ``hls::task`` compute tiles wired by a depth-2 ``stream_of_blocks`` (the SOBIF).
``Fill`` write-lock-fills a whole block from the ``x_in`` stream; ``Gather`` read-locks it and
random-accesses it (``b[p_in.read()]``) to produce ``y_out``.  The ping-pong lets Fill fill block
*j+1* while Gather random-reads block *j* — the overlap this phase exists to reproduce (~1301 cyc ≈
the ``(NJ+1)·N`` floor, not the ``NJ·2N`` serial).  Element-granular (one ``ap_uint<EW>`` per stream
transfer, indexing the block directly); the word-granular LW-unroll gather is Phase 4.

This isolates the SOBIF codegen + ping-pong overlap from memory composition (P2 already de-risked
m_axi): no MemRStream / Sequencer here.  The composite top is derived by the *same*
:func:`~examples.interleaver.composite_gen.composite_top_spec` — the only new thing is the
:class:`~examples.interleaver.composite_gen.SobEdge` for the block channel.

Run (project venv, from repo root)::

    PYTHONPATH=. pysilicon-venv/Scripts/python.exe examples/interleaver/sob_toy.py
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))

from waveflow.build.build import BuildConfig, BuildDag  # noqa: E402
from waveflow.build.streamutils import MemMgrStep, MemStreamStep, StreamUtilsStep  # noqa: E402
from waveflow.hw.clock import Clock  # noqa: E402
from waveflow.hw.hw_component import HwComponent, HwParam  # noqa: E402
from waveflow.hw.interface import (  # noqa: E402
    SobIFMaster,
    SobIFSlave,
    StreamOfBlocksIF,
    StreamIF,
    StreamIFMaster,
    StreamIFSlave,
)
from waveflow.hw.mem_stream import KernelTask  # noqa: E402
from waveflow.simulation.simobj import ProcessGen  # noqa: E402

from examples.interleaver.composite_gen import SobEdge, composite_top_spec  # noqa: E402
from examples.interleaver.mem_stream_gen import GEN_DIR, INCLUDE_DIR, render_tcl, render_top  # noqa: E402

DEFAULT_EW = 32     # element width (bits) — one ap_uint<32> per stream transfer (element-granular)
DEFAULT_N = 256     # block size (elements per SOBIF handover)


def _elem_dtype(ew: int) -> np.dtype:
    return np.dtype(np.uint32) if int(ew) <= 32 else np.dtype(np.uint64)


@dataclass
class Fill(HwComponent):
    """SOBIF **producer**: write-lock-fill a whole block from ``x_in`` (the sandbox ``load_task``).

    Endpoints: ``x_in`` (:class:`StreamIFSlave`, the boundary input), ``x_blk``
    (:class:`SobIFMaster`, the block edge to :class:`Gather`).  Codegen is the fixed ``fill_task``
    body; :meth:`run_proc` is the pysim golden."""

    cpp_kernel_name: ClassVar[str | None] = "fill"
    cpp_namespace: ClassVar[str | None] = "fill_impl"

    elem_bw: HwParam[int] = DEFAULT_EW
    block_n: HwParam[int] = DEFAULT_N
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        ew, n = int(self.elem_bw), int(self.block_n)
        self.x_in = StreamIFSlave(name=f"{self.name}_x_in", sim=self.sim, bitwidth=ew,
                                  has_tlast=False)
        self.x_blk = SobIFMaster(name=f"{self.name}_x_blk", sim=self.sim, bitwidth=ew, block_n=n)
        for ep in (self.x_in, self.x_blk):
            self.add_endpoint(ep)
        self._dtype = _elem_dtype(ew)

    def kernel_task(self) -> KernelTask:
        return KernelTask("fill_task", "fill_task.h", ("x_in", "x_blk"),
                          template_args=(int(self.elem_bw), int(self.block_n)))

    def run_proc(self) -> ProcessGen[None]:
        """pysim golden: pull one block's worth of words off ``x_in``, write-lock a fresh buffer,
        fill it, and commit — the producer half of the ping-pong."""
        n = int(self.block_n)
        while True:
            xwords = yield from self.x_in.get(nwords_max=n)          # one block (N words)
            buf = yield from self.x_blk.acquire_write()              # blocks if no free buffer
            buf[:xwords.shape[0]] = xwords.astype(self._dtype)
            yield from self.x_blk.commit_write(buf)                  # release to the consumer


@dataclass
class Gather(HwComponent):
    """SOBIF **consumer**: read-lock the block, random-access it with ``p_in`` indices -> ``y_out``
    (the sandbox ``gather_task``).

    Endpoints: ``p_in`` (:class:`StreamIFSlave` index stream), ``x_blk`` (:class:`SobIFSlave` block
    edge), ``y_out`` (:class:`StreamIFMaster`).  Codegen is the fixed ``gather_task`` body."""

    cpp_kernel_name: ClassVar[str | None] = "gather"
    cpp_namespace: ClassVar[str | None] = "gather_impl"

    elem_bw: HwParam[int] = DEFAULT_EW
    block_n: HwParam[int] = DEFAULT_N
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        ew, n = int(self.elem_bw), int(self.block_n)
        self.p_in = StreamIFSlave(name=f"{self.name}_p_in", sim=self.sim, bitwidth=ew,
                                  has_tlast=False)
        self.x_blk = SobIFSlave(name=f"{self.name}_x_blk", sim=self.sim, bitwidth=ew, block_n=n)
        self.y_out = StreamIFMaster(name=f"{self.name}_y_out", sim=self.sim, bitwidth=ew,
                                    has_tlast=False)
        for ep in (self.p_in, self.x_blk, self.y_out):
            self.add_endpoint(ep)
        self._dtype = _elem_dtype(ew)
        #: gather-completion times (cycles), one per block — the steady-state period / overlap probe.
        self.job_end_cyc: list[float] = []

    def kernel_task(self) -> KernelTask:
        return KernelTask("gather_task", "gather_task.h", ("p_in", "x_blk", "y_out"),
                          template_args=(int(self.elem_bw), int(self.block_n)))

    def run_proc(self) -> ProcessGen[None]:
        """pysim golden: read-lock a filled block, gather ``y[i]=b[p_in[i]]``, release, emit
        ``y_out`` — the consumer half of the ping-pong (random-access, element-granular)."""
        n = int(self.block_n)
        while True:
            buf = yield from self.x_blk.acquire_read()              # blocks until a block is ready
            pwords = yield from self.p_in.get(nwords_max=n)         # N indices
            y = buf[pwords.astype(np.int64)]                        # random gather
            yield from self.x_blk.release_read()                   # free the buffer for the producer
            yield from self.y_out.write(y.astype(self._dtype))
            self.job_end_cyc.append(self.now / self.clk.period)


@dataclass
class SobToy(HwComponent):
    """Hierarchical pure-AXIS ``Fill ->SOBIF-> Gather`` composite — the generated analogue of
    ``interleaver_sob_task.cpp``.

    Boundary: ``x_in`` / ``p_in`` (AXIS in), ``y_out`` (AXIS out).  One internal :class:`SobEdge`
    (``x_blk``) between the two tiles lowers to ``hls::stream_of_blocks<ap_uint<EW>[N], 2>``.  Passive
    at this level; :func:`composite_top_spec` derives the generated top from this graph."""

    cpp_kernel_name: ClassVar[str | None] = "sob_toy"
    cpp_namespace: ClassVar[str | None] = "sob_toy_impl"

    elem_bw: HwParam[int] = DEFAULT_EW
    block_n: HwParam[int] = DEFAULT_N
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        ew, n = int(self.elem_bw), int(self.block_n)

        self.fill = Fill(name=f"{self.name}_fill", sim=self.sim, elem_bw=ew, block_n=n, clk=self.clk)
        self.gather = Gather(name=f"{self.name}_gather", sim=self.sim, elem_bw=ew, block_n=n,
                             clk=self.clk)
        for c in (self.fill, self.gather):
            self.add_comp(c)
        self.ordered_subcomps = [self.fill, self.gather]

        # internal block edge: Fill (producer) -> SOBIF -> Gather (consumer).
        self._blk_if = StreamOfBlocksIF(
            name=f"{self.name}_x_blk_if", sim=self.sim, clk=self.clk, bitwidth=ew, block_n=n)
        self._blk_if.bind("master", self.fill.x_blk)
        self._blk_if.bind("slave", self.gather.x_blk)
        self.add_if(self._blk_if)

        self.internal_edges = [
            SobEdge("x_blk", self.fill.x_blk, self.gather.x_blk, elem_bw=ew, block_n=n),
        ]
        self.boundary = [
            ("x_in", self.fill.x_in, "axis_in", None),
            ("p_in", self.gather.p_in, "axis_in", None),
            ("y_out", self.gather.y_out, "axis_out", None),
        ]
        self.cmd_headers = ()                       # pure-AXIS: no command structs
        self.extra_includes = ("hls_streamofblocks.h",)

        # convenience refs for the sim harness
        self.x_in = self.fill.x_in
        self.p_in = self.gather.p_in
        self.y_out = self.gather.y_out


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

def gen_headers(config: BuildConfig) -> None:
    """Generate streamutils_hls.h + memmgr.hpp + the fixed compute-tile bodies into ``include/``
    (no DataSchema command structs — the toy is pure-AXIS with no command headers)."""
    inner = BuildDag()
    inner.add(StreamUtilsStep(output_dir=INCLUDE_DIR))
    inner.add(MemMgrStep(output_dir=INCLUDE_DIR))
    inner.add(MemStreamStep(output_dir=INCLUDE_DIR))    # copies fill_task.h / gather_task.h
    results = inner.run(config, force=True)
    failed = [n for n, r in results.items() if not r.success]
    if failed:
        raise RuntimeError(f"gen-include failed: {failed}")


def generate(out_dir: Path = HERE, elem_bw: int = DEFAULT_EW, block_n: int = DEFAULT_N) -> Path:
    """Generate headers + the SobToy composite top .cpp + its csynth .tcl into *out_dir*."""
    from waveflow.simulation.simulation import Simulation

    config = BuildConfig(root_dir=out_dir, params={})
    gen_headers(config)

    comp = SobToy(name="sob_toy", sim=Simulation(), elem_bw=elem_bw, block_n=block_n)
    # width unused for pure-AXIS edges/ports (they carry ap_uint<EW>); pass elem_bw so the axis
    # port decls use ap_uint<EW>.
    spec = composite_top_spec(comp, width=elem_bw)

    gen = out_dir / GEN_DIR
    gen.mkdir(parents=True, exist_ok=True)
    cpp = gen / f"{spec.top_name}.cpp"
    cpp.write_text(render_top(spec), encoding="utf-8")
    tcl = out_dir / f"{spec.top_name}.tcl"
    tcl.write_text(render_tcl(spec.top_name), encoding="utf-8")
    print(f"generated {cpp.relative_to(out_dir)} + {tcl.name}")
    return cpp


if __name__ == "__main__":
    generate()
