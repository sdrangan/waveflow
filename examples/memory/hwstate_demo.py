"""hwstate_demo.py — :class:`HwState`, storage the kernel owns.

Backs ``docs/guide/memory/hwstate.md``.  A two-lane running total: the smallest module that needs
storage to outlive a firing.  The demo shows both halves — the pysim behaviour (the total climbs
across firings) and the generated C++ (a ``static`` inside the task body, with its pragma).

For the RTL-verified version of this shape, see ``examples/state_toy`` and its XSI gate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np

from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataArray, IntField
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_state import HwState
from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
from waveflow.hw.synth import synthesizable
from waveflow.simulation.simobj import ProcessGen

Word32 = IntField.specialize(bitwidth=32, signed=False)


class Pair(DataArray):
    """The stream payload: two lanes per firing."""

    element_type = Word32
    static = True
    max_shape = (2,)


#: The state's storage.  ``cpp_storage="raw"`` is what lowers it to a bare ``ap_uint<32>[2]`` —
#: the form a ``static`` declaration wants (a struct would be legal C++ and the wrong shape).
TotalArray = DataArray.specialize(Word32, max_shape=(2,), cpp_storage="raw")


@dataclass
class RunningTotal(FreeRunMod):
    """``total += x``; emit ``total``.  One firing = one pair."""

    cpp_kernel_name: ClassVar[str | None] = "running_total"
    cpp_namespace: ClassVar[str | None] = "running_total_impl"

    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.x_in = StreamIFSlave(name=f"{self.name}_x_in", sim=self.sim,
                                  bitwidth=32, has_tlast=False)
        self.y_out = StreamIFMaster(name=f"{self.name}_y_out", sim=self.sim,
                                    bitwidth=32, has_tlast=False)
        self.add_endpoint(self.x_in)
        self.add_endpoint(self.y_out)

        # The declaration.  Without it, reading self.total from run_iter is an implicit capture
        # and the extractor refuses — it cannot tell a baked constant from a register.
        self.total = HwState(TotalArray(), partition={"type": "complete"})
        self.add_state(self.total)

    def run_iter(self) -> ProcessGen[None]:
        x = yield from self.x_in.get_schema(Pair)
        # The hook's result is named before it is written.  Not stylistic: the extractor accepts a
        # fixed list of statement shapes, and a call nested inside write(...) is not one of them.
        y = self.accumulate(x, self.total)
        yield from self.y_out.write(y)

    @synthesizable
    def accumulate(self, x: Pair, total: HwState) -> Pair:
        """The pysim twin of the hand-written hook.  ``total.val`` delegates to the wrapped array,
        so the ``HwState`` wrapper is invisible to the arithmetic."""
        total.val[:] = (np.asarray(total.val, dtype=np.uint64)
                        + np.asarray(x.val, dtype=np.uint64)) & 0xFFFFFFFF
        return Pair(np.asarray(total.val).copy())


def show_pysim() -> list[list[int]]:
    """Fire the module three times by hand and watch the total carry."""
    from waveflow.simulation.simulation import Simulation

    dut = RunningTotal(name="rt", sim=Simulation())
    seen = []
    for _ in range(3):
        out = dut.accumulate(Pair(np.array([1, 10], dtype=np.uint64)), dut.total)
        seen.append([int(v) for v in np.asarray(out.val)])
    print(f"pysim: three firings of [1, 10] -> {seen}")
    return seen


def show_codegen() -> str:
    """The generated task body — the declaration, its pragma, and the hook call."""
    from waveflow.build.hwgen import task_files_to_str

    src = task_files_to_str(RunningTotal)["running_total_task.h"]
    body = src[src.index("static void running_total_task"):src.index("#endif")]
    print("codegen:\n" + body.rstrip())
    return src


def main() -> None:
    seen = show_pysim()
    assert seen == [[1, 10], [2, 20], [3, 30]], seen
    src = show_codegen()
    assert "static ap_uint<32> total[2];" in src
    assert "#pragma HLS ARRAY_PARTITION variable=total complete dim=1" in src


if __name__ == "__main__":
    main()
