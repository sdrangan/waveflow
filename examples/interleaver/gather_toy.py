"""gather_toy.py — Phase-3 Gate-3 de-risk: Fill + Gather composition over SOBIF.

Pure-AXIS (no m_axi) proof-of-concept that demonstrates SOBIF + elem_read integration:
- Fill: StreamIF input → SOBIF write (gather words into blocks)
- Gather: SOBIF read → StreamIF output (read from blocks via random-access elem_read)

This is the minimal verification kernel for Phase 3 (stream-of-blocks interface).
Run (project venv, from repo root)::

    PYTHONPATH=. pysilicon-venv/Scripts/python.exe examples/interleaver/gather_toy.py
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

from waveflow.build.build import BuildConfig  # noqa: E402
from waveflow.hw.clock import Clock  # noqa: E402
from waveflow.hw.dataschema import DataArray, IntField  # noqa: E402
from waveflow.hw.hw_component import HwComponent, HwParam  # noqa: E402
from waveflow.hw.hw_composite import CompositeComp  # noqa: E402
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave, StreamOfBlocksIF, SobIFMaster, SobIFSlave  # noqa: E402
from waveflow.hw.mem_stream import KernelTask, WORD_BW_SUPPORTED  # noqa: E402
from waveflow.simulation.simobj import ProcessGen  # noqa: E402

# --- configuration -------
DEFAULT_MEM_DW = 64
GEN_DIR = "gen"
INCLUDE_DIR = "include"

# --- word type: element/word coordinate or count (for streaming) -------
Word32 = IntField.specialize(bitwidth=32, signed=False)

# --- block type: array of 64-bit words (typed block for SOBIF) -------
WordBlock = DataArray.specialize(
    element_type=IntField.specialize(bitwidth=64, signed=False),
    max_shape=(8,),
    member_name="words"
)


@dataclass
class Fill(HwComponent):
    """Pure-stream fill component: consume a word stream, gather into blocks over SOBIF.

    Endpoints: ``s_in`` (StreamIFSlave, word input), ``m_out`` (SobIFMaster, block output).

    Behavior: reads ``block_n`` words per cycle from s_in, writes one complete block to m_out,
    and repeats. One block per iteration (no partial blocks).
    """

    cpp_kernel_name: ClassVar[str | None] = "fill"
    cpp_namespace: ClassVar[str | None] = "fill_impl"

    mem_dwidth: HwParam[int] = 64
    block_n: HwParam[int] = 8  # elements per block
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)

        # Input stream (word-granular)
        self.s_in = StreamIFSlave(
            name=f"{self.name}_s_in", sim=self.sim, bitwidth=w, has_tlast=False)
        self.add_endpoint(self.s_in)

        # Output SOBIF master endpoint (typed block)
        self.m_out = SobIFMaster(name=f"{self.name}_m_out", sim=self.sim,
                                element_type=WordBlock)
        self.add_endpoint(self.m_out)

    def kernel_task(self) -> KernelTask:
        return KernelTask("fill", "fill.h", ("s_in", "m_out"),
                          template_args=(int(self.mem_dwidth), int(self.block_n)))

    def run_proc(self) -> ProcessGen[None]:
        """Pysim golden: read block_n words, commit one typed block, repeat."""
        bn = int(self.block_n)
        while True:
            block = yield from self.m_out.acquire_write()
            for i in range(bn):
                w: int = yield from self.s_in.get(int)
                block[i] = w
            yield from self.m_out.commit_write(block)


@dataclass
class Gather(HwComponent):
    """Pure-stream gather component: random-access read from SOBIF blocks, emit word stream.

    Endpoints: ``s_in`` (SobIFSlave, block input), ``m_out`` (StreamIFMaster, word output).

    Behavior: reads a block, random-accesses it in a fixed order (0, 1, 2, ..., block_n-1),
    and emits words to the output stream. For parity with the interleaver, this is the simplest
    gather (identity gather). The key is that it exercises random-access on the block input.
    """

    cpp_kernel_name: ClassVar[str | None] = "gather"
    cpp_namespace: ClassVar[str | None] = "gather_impl"

    mem_dwidth: HwParam[int] = 64
    block_n: HwParam[int] = 8
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)

        # Input SOBIF slave endpoint (typed block)
        self.s_in = SobIFSlave(name=f"{self.name}_s_in", sim=self.sim,
                              element_type=WordBlock)
        self.add_endpoint(self.s_in)

        # Output stream (word-granular)
        self.m_out = StreamIFMaster(
            name=f"{self.name}_m_out", sim=self.sim, bitwidth=w, has_tlast=False)
        self.add_endpoint(self.m_out)

    def kernel_task(self) -> KernelTask:
        return KernelTask("gather", "gather.h", ("s_in", "m_out"),
                          template_args=(int(self.mem_dwidth), int(self.block_n)))

    def run_proc(self) -> ProcessGen[None]:
        """Pysim golden: read a block, emit words in order (identity gather for now)."""
        bn = int(self.block_n)
        while True:
            block: np.ndarray = yield from self.s_in.acquire_read()
            for i in range(bn):
                yield from self.m_out.write(int(block[i]))
            yield from self.s_in.release_read()


@dataclass
class GatherToy(CompositeComp):
    """Minimal pure-AXIS kernel: Fill -> SOBIF -> Gather (no m_axi, no compute).

    Endpoints (boundary): ``s_in`` (word input), ``m_out`` (word output).
    Sub-components: Fill and Gather wired over an internal SOBIF.

    This is the Gate-3 verification kernel: proves SOBIF codegen works and elem_read/write
    integration is correct. Expected steady-state: ~1301 cycles (from sob_task baseline).
    """

    cpp_kernel_name: ClassVar[str | None] = "gather_toy"
    cpp_namespace: ClassVar[str | None] = "gather_toy_impl"

    mem_dwidth: HwParam[int] = 64
    block_n: HwParam[int] = 8
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)

        # Sub-components
        self.fill = Fill(name=f"{self.name}_fill", sim=self.sim, mem_dwidth=w,
                         block_n=int(self.block_n), clk=self.clk)
        self.gather = Gather(name=f"{self.name}_gather", sim=self.sim, mem_dwidth=w,
                             block_n=int(self.block_n), clk=self.clk)
        for c in (self.fill, self.gather):
            self.add_comp(c)
        self.ordered_subcomps = [self.fill, self.gather]

        # Internal SOBIF interface (connects Fill.m_out -> Gather.s_in)
        self._sob_if = StreamOfBlocksIF(
            name=f"{self.name}_sob_if",
            sim=self.sim,
            clk=self.clk,
            element_type=WordBlock,
        )
        self._sob_if.bind("master", self.fill.m_out)
        self._sob_if.bind("slave", self.gather.s_in)
        self.add_if(self._sob_if)

        # Define internal edges for codegen (compatible with composite_top_spec)
        from waveflow.build.composite_gen import SobEdge
        self.internal_edges = [
            SobEdge("blk", self.fill.m_out, self.gather.s_in, elem_bw=w, block_n=int(self.block_n)),
        ]

        # Define boundary ports for codegen
        self.boundary = [
            ("s_in", self.fill.s_in),
            ("m_out", self.gather.m_out),
        ]

        # Boundary (convenience refs)
        self.s_in = self.fill.s_in
        self.m_out = self.gather.m_out


if __name__ == "__main__":
    from waveflow.build.elaborate import elaborate

    # Minimal header generation and elaboration (pysim only for now)
    config = BuildConfig(root_dir=HERE)
    comp = elaborate(GatherToy, {"mem_dwidth": DEFAULT_MEM_DW, "block_n": 8}, name="gather_toy")
    print(f"Elaborated {comp.cpp_kernel_name}: {comp}")
