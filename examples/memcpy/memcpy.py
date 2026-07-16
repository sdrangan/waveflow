"""memcpy.py — Pure memory copy example: MemRStream → identity copy → MemWStream.

Minimal composite demonstrating memory-to-memory transfer without computation.
Reads N words from gmem0, copies them unchanged, writes to gmem1.

Run (project venv, from repo root)::

    PYTHONPATH=. pysilicon-venv/Scripts/python.exe examples/memcpy/memcpy.py
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

from waveflow.hw.clock import Clock  # noqa: E402
from waveflow.hw.hw_component import HwParam  # noqa: E402
from waveflow.hw.hw_composite import CompositeComp  # noqa: E402
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave  # noqa: E402
from waveflow.hw.mem_stream import MemRStream, MemWStream, MRCmd, MemComplete  # noqa: E402
from waveflow.simulation.simobj import ProcessGen  # noqa: E402

# --- configuration -------
DEFAULT_MEM_DW = 64
DEFAULT_MEM_AWIDTH = 32

GEN_DIR = "gen"
INCLUDE_DIR = "include"


@dataclass
class MemcpyIdentity(CompositeComp):
    """Minimal memcpy: read → copy → write (no computation).

    Endpoints (boundary):
    - ``s_cmd`` (StreamIFSlave, memcpy commands: off_in, off_out, n_words)
    - ``m_in`` (MMIFMaster, gmem0 read)
    - ``m_out`` (MMIFMaster, gmem1 write)

    Sub-components:
    - MemRStream: reads from gmem0 into word stream
    - MemWStream: writes word stream to gmem1
    - (Identity copy: direct StreamIF path between them)

    This is the canonical pure-memory workload: baseline for memory-bound kernels.
    Expected throughput: ~100% bus utilization (read+write overlap via full-duplex bundle).
    """

    cpp_kernel_name: ClassVar[str | None] = "memcpy"
    cpp_namespace: ClassVar[str | None] = "memcpy_impl"

    mem_dwidth: HwParam[int] = DEFAULT_MEM_DW
    mem_awidth: HwParam[int] = DEFAULT_MEM_AWIDTH
    max_xfer_len: HwParam[int] = 8
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        w = int(self.mem_dwidth)
        aw = int(self.mem_awidth)
        max_xfer = int(self.max_xfer_len)

        # Sub-components: read and write memory streams
        self.mem_r = MemRStream(
            name=f"{self.name}_mem_r", sim=self.sim, mem_dwidth=w,
            mem_awidth=aw, max_xfer_len=max_xfer, emit_done=True, clk=self.clk
        )
        self.mem_w = MemWStream(
            name=f"{self.name}_mem_w", sim=self.sim, mem_dwidth=w,
            mem_awidth=aw, max_xfer_len=max_xfer, clk=self.clk
        )
        for c in (self.mem_r, self.mem_w):
            self.add_comp(c)
        self.ordered_subcomps = [self.mem_r, self.mem_w]

        # Internal StreamIF: identity copy (read words → write words)
        copy_if = StreamIF(
            name=f"{self.name}_copy_if", sim=self.sim, clk=self.clk, bitwidth=w
        )
        copy_if.bind("master", self.mem_r.m_out)
        copy_if.bind("slave", self.mem_w.s_in)
        self.add_if(copy_if)

        # Boundary: command and memory ports
        self.s_cmd = self.mem_r.s_cmd
        self.m_in = self.mem_r.m_mem
        self.m_out = self.mem_w.m_mem
        self.s_done = self.mem_r.s_done  # completion signal

        # Define internal edges for codegen
        from examples.interleaver.composite_gen import StreamEdge
        self.internal_edges = [
            StreamEdge("copy", self.mem_r.m_out, self.mem_w.s_in),
        ]

        # Define boundary ports for codegen
        self.boundary = [
            ("s_cmd", self.mem_r.s_cmd, "axis_in", None),
            ("m_in", self.mem_r.m_mem, "maxi_read", "gmem0"),
            ("m_out", self.mem_w.m_mem, "maxi_write", "gmem1"),
            ("s_done", self.mem_r.s_done, "axis_out", None),
        ]


if __name__ == "__main__":
    from waveflow.build.elaborate import elaborate

    # Elaborate and print the structure
    comp = elaborate(
        MemcpyIdentity,
        {"mem_dwidth": DEFAULT_MEM_DW, "mem_awidth": DEFAULT_MEM_AWIDTH, "max_xfer_len": 8},
        name="memcpy"
    )
    print(f"Elaborated {comp.name}:")
    print(f"  Sub-components: {list(comp.sub_comps.keys())}")
    print(f"  Interfaces: {list(comp.interfaces.keys())}")
    print(f"  Boundary: {[name for name, _, _, _ in comp.boundary]}")
