"""memcpy_sim.py — Pysim harness for memcpy: verify MemRStream/MemWStream flow.

Generates test data, allocates in memory, runs MemRStream+identity+MemWStream,
verifies identity copy (input data == output data, same order).

STATUS: WORK IN PROGRESS — DOES NOT PASS.  As of 2026-07-16 this harness reports 32/32
mismatches (destination reads back all zeros), and the debug prints show the CmdDriver
run_proc bodies firing more than once.  The data never reaches the destination region.
The bug is believed to be in this harness (command/crossbar wiring or sim-object
registration), NOT in MemcpyIdentity itself: the pytest coverage in
tests/examples/test_mem_copy.py exercises MemRStream/MemWStream and passes.  Do not treat
a run of this script as evidence about the memcpy components until it is debugged.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Generator

import numpy as np

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
sys.path.insert(0, str(REPO))

from waveflow.build.elaborate import elaborate  # noqa: E402
from waveflow.hw.clock import Clock  # noqa: E402
from waveflow.hw.memory import MemComponent, AddrUnit  # noqa: E402
from waveflow.hw.mem_stream import MRCmd, MWCmd  # noqa: E402
from waveflow.hw.dataschema import IntField  # noqa: E402
from waveflow.simulation.simulation import Simulation  # noqa: E402
from waveflow.simulation.simobj import ProcessGen, SimObj  # noqa: E402
from waveflow.hw.interface import StreamIFMaster  # noqa: E402

from examples.memcpy.memcpy import MemcpyIdentity  # noqa: E402

DEFAULT_MEM_DW = 64


@dataclass
class CmdDriverRead(SimObj):
    """Generates MemRStream read commands."""

    src_addr: int = 0
    n_words: int = 0

    def __post_init__(self) -> None:
        super().__post_init__()
        from waveflow.hw.interface import StreamIFMaster
        self.m_cmd = StreamIFMaster(
            name=f"{self.name}_m_cmd", sim=self.sim, bitwidth=DEFAULT_MEM_DW, has_tlast=False
        )

    def run_proc(self) -> Generator:
        """Emit one read command: read N words from src."""
        print(f"[CmdDriverRead.run_proc] Starting")
        cmd = MRCmd(addr=self.src_addr, len=self.n_words)
        print(f"[CmdDriverRead.run_proc] Writing MRCmd")
        yield from self.m_cmd.write(cmd)
        print(f"[CmdDriverRead] Sent read command: addr={self.src_addr}, len={self.n_words}")


@dataclass
class CmdDriverWrite(SimObj):
    """Generates MemWStream write commands."""

    dst_addr: int = 0
    n_words: int = 0

    def __post_init__(self) -> None:
        super().__post_init__()
        from waveflow.hw.interface import StreamIFMaster
        self.m_cmd = StreamIFMaster(
            name=f"{self.name}_m_cmd", sim=self.sim, bitwidth=DEFAULT_MEM_DW, has_tlast=False
        )

    def run_proc(self) -> Generator:
        """Emit one write command: write N words to dst."""
        print(f"[CmdDriverWrite.run_proc] Starting")
        cmd = MWCmd(addr=self.dst_addr, len=self.n_words)
        print(f"[CmdDriverWrite.run_proc] Writing MWCmd")
        yield from self.m_cmd.write(cmd)
        print(f"[CmdDriverWrite] Sent write command: addr={self.dst_addr}, len={self.n_words}")


def run_sim(n_words: int = 32, seed: int = 42) -> bool:
    """Run pysim: generate test data, run memcpy, verify identity copy."""
    np.random.seed(seed)

    # Generate test pattern (random uint64 words)
    test_data = np.random.randint(0, 2**64, size=n_words, dtype=np.uint64)

    # Elaborate the composite
    memcpy = elaborate(
        MemcpyIdentity,
        {"mem_dwidth": DEFAULT_MEM_DW, "mem_awidth": 32, "max_xfer_len": 8},
        name="memcpy_sim",
    )

    # Run simulation
    with Simulation().as_current() as sim:
        # Create one shared memory for both source and destination regions
        # Layout: [src: n_words] [dst: n_words]
        word_type = IntField.specialize(bitwidth=64, signed=False)
        arena_words = 2 * n_words
        mem = MemComponent(
            name="mem",
            sim=sim,
            word_size=DEFAULT_MEM_DW,
            addr_size=32,
            nwords_tot=arena_words,
            latency_init=0,
            latency_per_word=0,
            inline=False,
            clk=memcpy.clk,
        )

        # Allocate and populate regions
        src_addr = mem.alloc_array(test_data, elem_type=word_type, count=n_words)
        dst_addr = mem.alloc_array(np.zeros(n_words, dtype=np.uint64), elem_type=word_type, count=n_words)

        print(f"\nTest setup:")
        print(f"  Shared memory arena: {arena_words} words")
        print(f"  Source: addr={src_addr}, n_words={n_words}")
        print(f"  Dest:   addr={dst_addr}, n_words={n_words}")
        print(f"  Test data (first 8): {test_data[:8]}")

        # Create read and write command drivers
        cmd_driver_r = CmdDriverRead(
            name="cmd_driver_r",
            sim=sim,
            src_addr=src_addr // (DEFAULT_MEM_DW // 8),  # Convert byte addr to word addr
            n_words=n_words,
        )
        cmd_driver_w = CmdDriverWrite(
            name="cmd_driver_w",
            sim=sim,
            dst_addr=dst_addr // (DEFAULT_MEM_DW // 8),  # Convert byte addr to word addr
            n_words=n_words,
        )

        # Bind command inputs
        from waveflow.hw.interface import StreamIF
        from waveflow.hw.memif import AXIMMCrossBarIF, assign_address_ranges
        cmd_if_r = StreamIF(name="cmd_if_r", sim=sim, clk=memcpy.clk, bitwidth=DEFAULT_MEM_DW, has_tlast=False)
        cmd_if_r.bind(ep_name="master", endpoint=cmd_driver_r.m_cmd)
        cmd_if_r.bind(ep_name="slave", endpoint=memcpy.s_cmd_r)

        cmd_if_w = StreamIF(name="cmd_if_w", sim=sim, clk=memcpy.clk, bitwidth=DEFAULT_MEM_DW, has_tlast=False)
        cmd_if_w.bind(ep_name="master", endpoint=cmd_driver_w.m_cmd)
        cmd_if_w.bind(ep_name="slave", endpoint=memcpy.s_cmd_w)

        # Bind memory ports via crossbar
        xbar = AXIMMCrossBarIF(sim=sim, clk=memcpy.clk, nports_master=2, nports_slave=1, bitwidth=DEFAULT_MEM_DW)
        xbar.bind("master_0", memcpy.m_in)   # MemRStream read
        xbar.bind("master_1", memcpy.m_out)  # MemWStream write
        xbar.bind("slave_0", mem.s_mm)
        assign_address_ranges([mem.s_mm], [(0, arena_words * (DEFAULT_MEM_DW // 8))])

        # Add objects to simulation
        sim.add_obj(memcpy)
        # Add internal components of the composite
        sim.add_obj(memcpy.mem_r)
        sim.add_obj(memcpy.mem_w)
        sim.add_obj(mem)
        sim.add_obj(cmd_driver_r)
        sim.add_obj(cmd_driver_w)

        # Run simulation
        sim.run_sim()

        # Read output data from destination region
        output_data = mem.read_array(dst_addr, elem_type=word_type, count=n_words)

        # Verify: output should match input (identity copy)
        if np.array_equal(output_data, test_data):
            print(f"\n[PASS] Output matches input ({n_words} words)")
            print(f"  First 8:  {output_data[:8]}")
            print(f"  Last 8:   {output_data[-8:]}")
            return True
        else:
            print(f"\n[FAIL] Output mismatch!")
            print(f"  Expected {n_words} words")
            print(f"  First 8 expected: {test_data[:8]}")
            print(f"  First 8 got:      {output_data[:8]}")
            mismatches = np.sum(output_data != test_data)
            print(f"  Mismatches: {mismatches}")
            return False


if __name__ == "__main__":
    success = run_sim(n_words=32, seed=42)
    sys.exit(0 if success else 1)
