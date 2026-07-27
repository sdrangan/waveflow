"""
mem_demo.py — read-modify-write over AXI-MM to a memory SimObj.

This is the smallest end-to-end proof that :class:`MemoryMod` is a real
SimPy participant: a driver issues AXI-MM transactions across a ``DirectMMIF``
to the memory's ``s_mm`` slave, and the memory's modeled *access* latency
composes with the interconnect's *bus* latency.

Topology
--------
  MemDriver (master) ── DirectMMIF ── MemoryMod.s_mm   (latency-modeling)

                         bus latency  +  access latency
                         (DirectMMIF)    (MemoryMod)

Scenario
--------
  1. allocate a region in the memory (inline=False / external-DDR style)
  2. write a known array (typed, via write_array)
  3. read it back and verify
  4. modify in-place (+1) and write the result back
  5. read again and verify; print transfer times (showing the access latency)

A second, optional driver over an ``AXIMMCrossBarIF`` demonstrates two masters
serializing on the one memory.

Run standalone::

    python -m examples.memory.mem_demo
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import IntField
from waveflow.hw.memif import (
    AXIMMCrossBarIF,
    DirectMMIF,
    MMIFMaster,
    assign_address_ranges,
)
from waveflow.hw.memory import MemoryMod
from waveflow.simulation.simobj import ProcessGen, SimObj
from waveflow.simulation.simulation import Simulation


Uint32 = IntField.specialize(bitwidth=32, signed=False)


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

@dataclass
class MemDriver(SimObj):
    """Drives a read-modify-write sequence against a memory over AXI-MM."""

    master: MMIFMaster | None = None
    base_addr: int = 0
    values: np.ndarray = field(default_factory=lambda: np.arange(8, dtype=np.uint32))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.passed: bool = False
        self.read_back: np.ndarray | None = None
        self.modified_back: np.ndarray | None = None

    def run_proc(self) -> ProcessGen[None]:
        n = len(self.values)
        addr = self.base_addr

        # --- 2. write a known array (typed) ---
        t0 = self.now
        yield from self.master.write_array(self.values, Uint32, addr)
        print(f"[{self.name}] write_array({n}) @0x{addr:04x}: "
              f"done at t={self.now:.4f} (dt={self.now - t0:.4f})")

        # --- 3. read back and verify ---
        t0 = self.now
        self.read_back = yield from self.master.read_array(Uint32, count=n, addr=addr)
        print(f"[{self.name}] read_array({n})  @0x{addr:04x}: "
              f"{self.read_back} at t={self.now:.4f} (dt={self.now - t0:.4f})")
        ok_read = np.array_equal(self.read_back, self.values)

        # --- 4. modify (+1) and write back ---
        modified = self.read_back + 1
        t0 = self.now
        yield from self.master.write_array(modified, Uint32, addr)
        print(f"[{self.name}] write_array(+1)  @0x{addr:04x}: "
              f"done at t={self.now:.4f} (dt={self.now - t0:.4f})")

        # --- 5. read again and verify ---
        self.modified_back = yield from self.master.read_array(Uint32, count=n, addr=addr)
        ok_modified = np.array_equal(self.modified_back, self.values + 1)

        self.passed = bool(ok_read and ok_modified)


# ---------------------------------------------------------------------------
# Harness — single driver over DirectMMIF (the required deliverable)
# ---------------------------------------------------------------------------

class MemDemo:
    """Wires one MemDriver to one MemoryMod over a DirectMMIF."""

    def __init__(self) -> None:
        self.sim = Simulation()
        self.clk = Clock(freq=100.0)   # 100 Hz → 1 cycle = 0.01 s

        # Latency-modeling memory: fixed 4-cycle access + 1 cycle/word.
        self.mem = MemoryMod(
            name="mem", sim=self.sim, inline=False, clk=self.clk,
            latency_init=4.0, latency_per_word=1.0,
        )
        values = np.arange(8, dtype=np.uint32) + 0x10

        # Carve out a region up front; the driver writes to its byte address.
        self.base_addr = self.mem.alloc(len(values))

        self.driver = MemDriver(
            sim=self.sim, base_addr=self.base_addr, values=values,
            master=MMIFMaster(sim=self.sim, bitwidth=32),
        )

        # Bus latency on the interconnect — composes with the memory's access
        # latency, it does not replace it.
        self.direct = DirectMMIF(
            sim=self.sim, clk=self.clk,
            latency_write=2.0, latency_read=2.0, latency_read_return=2.0,
        )
        self.direct.bind("master", self.driver.master)
        self.direct.bind("slave", self.mem.s_mm)

    def run_and_check(self) -> bool:
        print("=== memory SimObj read-modify-write demo (DirectMMIF) ===")
        self.sim.run_sim()
        assert self.driver.passed, "read-modify-write mismatch"
        assert np.array_equal(self.driver.read_back, self.driver.values)
        assert np.array_equal(self.driver.modified_back, self.driver.values + 1)
        print("All checks passed.\n")
        return self.driver.passed


# ---------------------------------------------------------------------------
# Optional harness — two drivers serializing on one memory over a crossbar
# ---------------------------------------------------------------------------

class MemCrossbarDemo:
    """Two MemDrivers share one MemoryMod through an AXIMMCrossBarIF."""

    MEM_BASE = 0x0000
    MEM_SIZE = 0x1000

    def __init__(self) -> None:
        self.sim = Simulation()
        self.clk = Clock(freq=100.0)

        self.mem = MemoryMod(
            name="mem", sim=self.sim, inline=False, clk=self.clk,
            latency_init=4.0, latency_per_word=1.0,
        )
        # Two disjoint regions, one per driver.
        a0 = self.mem.alloc(8)
        a1 = self.mem.alloc(8)

        self.xbar = AXIMMCrossBarIF(
            sim=self.sim, clk=self.clk,
            nports_master=2, nports_slave=1, bitwidth=32,
            latency_init=2.0, latency_read_return=2.0,
        )

        self.drivers = [
            MemDriver(sim=self.sim, base_addr=a0,
                      values=np.arange(8, dtype=np.uint32) + 0x100,
                      master=MMIFMaster(sim=self.sim, bitwidth=32)),
            MemDriver(sim=self.sim, base_addr=a1,
                      values=np.arange(8, dtype=np.uint32) + 0x200,
                      master=MMIFMaster(sim=self.sim, bitwidth=32)),
        ]
        self.xbar.bind("master_0", self.drivers[0].master)
        self.xbar.bind("master_1", self.drivers[1].master)
        self.xbar.bind("slave_0", self.mem.s_mm)
        assign_address_ranges([self.mem.s_mm], [(self.MEM_BASE, self.MEM_SIZE)])

    def run_and_check(self) -> bool:
        print("=== memory SimObj demo (AXIMMCrossBarIF, 2 masters) ===")
        self.sim.run_sim()
        ok = all(d.passed for d in self.drivers)
        assert ok, "crossbar read-modify-write mismatch"
        print("All checks passed.\n")
        return ok


def run_and_check() -> bool:
    """Run both harnesses and self-check; returns True on success."""
    ok_direct = MemDemo().run_and_check()
    ok_xbar = MemCrossbarDemo().run_and_check()
    return ok_direct and ok_xbar


if __name__ == "__main__":
    run_and_check()
