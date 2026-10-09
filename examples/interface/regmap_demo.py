"""
regmap_demo.py — VitisRegMap + VitisRegMapMMIFSlave demonstration.

Topology
--------
  CPU ──── AXIMMCrossBarIF (LITE) ──── FakeAccel (VitisRegMapMMIFSlave)
   ^                                        |
   └────────────── IrqIF ───────────────────┘   (the kernel's interrupt line)

Scenario
--------
A "fake accelerator" exposes a VitisRegMap with four user fields:

  status_clear  W1C   host writes 1 to clear halted/error
  halted        R     kernel sets 1 on error
  error         R     DemoError enum: OK / BAD_INPUT / OVERFLOW
  coeff_pair    RW    2-element array of uint32

The kernel (on_start) sleeps 5 cycles, reads coeff_pair, and either:
  * [0, 0]        → BAD_INPUT
  * sum > 1_000   → OVERFLOW
  * else          → success (halted remains 0, error = OK)

CPU sequence
------------
Each launch is ``ap_start`` followed by a sleep on the kernel's interrupt (enabled once, cleared
after each wake) -- the CPU never reads ap_done.

  1. coeff_pair=[0,0]        → launch → expect BAD_INPUT, halted=1
  2. clear → coeff_pair=[600,700] → launch → expect OVERFLOW, halted=1
  3. clear → coeff_pair=[10,20]  → launch → expect OK, halted=0
  4. Try host write to halted (R-only) → expect RegMapAccessError
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any

from waveflow.hw.aximm import (
    AXIMMCrossBarIF,
    AXIMMProtocol,
    MMIFMaster,
    assign_address_ranges,
)
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataArray, EnumField, IntField
from waveflow.hw.irq import IrqIF, IrqIFSink
from waveflow.hw.regmap import (
    Bit,
    RegAccess,
    RegField,
    RegMapAccessError,
    VitisRegMap,
    VitisRegMapMMIFSlave,
)
from waveflow.simulation.simobj import ProcessGen, SimObj
from waveflow.simulation.simulation import Simulation


# ---------------------------------------------------------------------------
# Demo types
# ---------------------------------------------------------------------------


class DemoError(IntEnum):
    OK        = 0
    BAD_INPUT = 1
    OVERFLOW  = 2


DemoErrorField = EnumField.specialize(enum_type=DemoError)
Uint32            = IntField.specialize(bitwidth=32, signed=False)


class CoeffPairArray(DataArray):
    """2-element array of 32-bit unsigned ints."""

    element_type = Uint32
    max_shape    = (2,)
    static       = True


# ---------------------------------------------------------------------------
# Fake accelerator (kernel side)
# ---------------------------------------------------------------------------


@dataclass
class FakeAccel(SimObj):
    """A minimal accelerator component using VitisRegMap."""

    SLEEP_CYCLES: int = 5

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        super().__post_init__()

        # Build the register map with hooks.
        self.regmap = VitisRegMap({
            "status_clear": RegField(
                Bit,
                RegAccess.W1C,
                description="Clear halted/error",
                on_write=self._on_status_clear,
            ),
            "halted": RegField(
                Bit,
                RegAccess.R,
                description="1 = halted on error",
            ),
            "error": RegField(
                DemoErrorField,
                RegAccess.R,
                description="Last error code",
            ),
            "coeff_pair": RegField(
                CoeffPairArray,
                RegAccess.RW,
                description="Input coefficients",
            ),
        })

        self.slave = VitisRegMapMMIFSlave(
            name=f"{self.name}_slave",
            sim=self.sim,
            bitwidth=32,
            regmap=self.regmap,
            on_start=self.on_start,
        )

    def _on_status_clear(self, name: str, sub_word: int, raw_val: int) -> None:
        """W1C hook: clears halted and error when host writes status_clear."""
        self.regmap.set("halted", 0)
        self.regmap.set("error",  DemoError.OK)

    def on_start(self) -> ProcessGen[None]:
        """Kernel body: sleep, read coefficients, set status."""
        yield self.timeout(self.SLEEP_CYCLES)

        coeffs = self.regmap.get("coeff_pair")
        c0, c1 = int(coeffs.val.flat[0]), int(coeffs.val.flat[1])

        if c0 == 0 and c1 == 0:
            self.regmap.set("error",  DemoError.BAD_INPUT)
            self.regmap.set("halted", 1)
            return

        total = c0 + c1
        if total > 1000:
            self.regmap.set("error",  DemoError.OVERFLOW)
            self.regmap.set("halted", 1)
            return

        # Success: error stays OK, halted stays 0.
        self.regmap.set("error",  DemoError.OK)


# ---------------------------------------------------------------------------
# CPU model
# ---------------------------------------------------------------------------


@dataclass
class CPU(SimObj):
    """Issues a scripted sequence of register-map transactions."""

    accel: FakeAccel = None  # type: ignore[assignment]
    base_addr: int = 0x2000

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)

    def __post_init__(self) -> None:
        super().__post_init__()
        self.master = MMIFMaster(sim=self.sim, bitwidth=32)
        self.irq = IrqIFSink(name=f"{self.name}_irq", sim=self.sim)
        self.passed = False
        self._rm = None

    def _addr(self, field: str) -> int:
        return self.base_addr + self.accel.regmap.offset_of(field)

    def _read_halted(self) -> ProcessGen[int]:
        val = yield from self.master.read_schema(Bit, addr=self._addr("halted"))
        return val.val

    def _read_error(self) -> ProcessGen[DemoError]:
        val = yield from self.master.read_schema(DemoErrorField, addr=self._addr("error"))
        return DemoError(val.val)

    def _launch_and_wait(self) -> ProcessGen[None]:
        """Launch, then sleep until the kernel's interrupt fires (and clear it)."""
        if self._rm is None:
            self._rm = self.accel.regmap.bind_master(self.master, base_addr=self.base_addr)
        yield from self._rm.run(self.irq)

    def _clear_status(self) -> ProcessGen[None]:
        yield from self.master.write_schema(
            Bit(1), addr=self._addr("status_clear")
        )

    def run_proc(self) -> ProcessGen[None]:
        # ------------------------------------------------------------------
        # Scenario 1: coeff_pair = [0, 0] → BAD_INPUT
        # ------------------------------------------------------------------
        print("\n[CPU] Scenario 1: coeff_pair=[0,0] → expect BAD_INPUT")
        yield from self.master.write_schema(
            CoeffPairArray([0, 0]), addr=self._addr("coeff_pair")
        )
        yield from self._launch_and_wait()

        halted = yield from self._read_halted()
        error  = yield from self._read_error()
        print(f"[CPU]   halted={halted}  error={error.name}")
        assert halted == 1,              f"Expected halted=1, got {halted}"
        assert error == DemoError.BAD_INPUT, f"Expected BAD_INPUT, got {error.name}"
        print("[CPU]   ✓ BAD_INPUT verified")

        # ------------------------------------------------------------------
        # Scenario 2: clear, coeff_pair = [600, 700] → OVERFLOW
        # ------------------------------------------------------------------
        print("\n[CPU] Scenario 2: status_clear → coeff_pair=[600,700] → expect OVERFLOW")
        yield from self._clear_status()
        yield from self.master.write_schema(
            CoeffPairArray([600, 700]), addr=self._addr("coeff_pair")
        )
        yield from self._launch_and_wait()

        halted = yield from self._read_halted()
        error  = yield from self._read_error()
        print(f"[CPU]   halted={halted}  error={error.name}")
        assert halted == 1,               f"Expected halted=1, got {halted}"
        assert error == DemoError.OVERFLOW, f"Expected OVERFLOW, got {error.name}"
        print("[CPU]   ✓ OVERFLOW verified")

        # ------------------------------------------------------------------
        # Scenario 3: clear, coeff_pair = [10, 20] → success
        # ------------------------------------------------------------------
        print("\n[CPU] Scenario 3: status_clear → coeff_pair=[10,20] → expect OK")
        yield from self._clear_status()
        yield from self.master.write_schema(
            CoeffPairArray([10, 20]), addr=self._addr("coeff_pair")
        )
        yield from self._launch_and_wait()

        halted = yield from self._read_halted()
        error  = yield from self._read_error()
        print(f"[CPU]   halted={halted}  error={error.name}")
        assert halted == 0,          f"Expected halted=0, got {halted}"
        assert error == DemoError.OK, f"Expected OK, got {error.name}"
        print("[CPU]   ✓ OK verified")

        # ------------------------------------------------------------------
        # Scenario 4: host write to R-only field → RegMapAccessError
        # ------------------------------------------------------------------
        print("\n[CPU] Scenario 4: host write to 'halted' (R-only) → expect error")
        caught = False
        try:
            yield from self.master.write_schema(Bit(1), addr=self._addr("halted"))
        except RegMapAccessError as exc:
            caught = True
            print(f"[CPU]   caught RegMapAccessError: {exc}")
        assert caught, "Expected RegMapAccessError was not raised"
        print("[CPU]   ✓ access-mode enforcement verified")

        self.passed = True
        print("\n[CPU] All scenarios passed.")


# ---------------------------------------------------------------------------
# Demo harness
# ---------------------------------------------------------------------------


class RegMapDemo:
    """Wires the components and runs the simulation."""

    ACCEL_BASE = 0x2000
    ACCEL_SIZE = 0x100   # generous; actual size is ~7 words = 0x1C bytes

    def __init__(self) -> None:
        self.sim = Simulation()
        self.clk = Clock(freq=1.0)   # 1 Hz → 1 cycle = 1 s

        self.accel = FakeAccel(sim=self.sim)
        self.cpu   = CPU(sim=self.sim, accel=self.accel, base_addr=self.ACCEL_BASE)

        self.xbar = AXIMMCrossBarIF(
            sim=self.sim,
            clk=self.clk,
            nports_master=1,
            nports_slave=1,
            bitwidth=32,
            latency_init=0.0,
            latency_read_return=0.0,
        )
        self.xbar.bind("master_0", self.cpu.master)
        self.xbar.bind("slave_0",  self.accel.slave, protocol=AXIMMProtocol.LITE)

        self.irq = IrqIF(name="accel_irq", sim=self.sim)
        self.irq.bind("source", self.accel.slave.interrupt())
        self.irq.bind("sink", self.cpu.irq)

        assign_address_ranges(
            [self.accel.slave],
            [(self.ACCEL_BASE, self.ACCEL_SIZE)],
        )

    def run(self) -> None:
        print("=== regmap_demo: VitisRegMap demonstration ===")
        self.sim.run_sim()
        if not self.cpu.passed:
            raise AssertionError("CPU did not complete all scenarios successfully.")
        print("\n=== All checks passed. ===")


if __name__ == "__main__":
    RegMapDemo().run()
