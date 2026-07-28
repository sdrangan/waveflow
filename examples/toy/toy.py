"""toy.py — the minimal ``FreeRunMod`` components the guide quotes (a standalone and a composite).

These are **toys**: the smallest components that exercise each kind — a *standalone* ``FreeRunMod``
(a ``run_iter`` body) and a *composite* ``FreeRunMod`` (sub-components, no body) — kept small enough
to read in one screen and to quote verbatim in ``docs/guide/components/``.  They are backed by
``tests/examples/test_toy.py``, so the code on those pages is executed by CI and cannot silently rot.

What they claim: *this is real code, it runs, and the docs match it* — a tested pysim model written
in synthesizable form.  What they do **not** claim: *this synthesizes*.  No ``FreeRunMod`` is
auto-extracted today (``MemRStream``/``MemWStream`` hand off fixed hand-written ``hls::task`` bodies
via ``kernel_task()``; their ``run_iter`` is pysim-golden only).  See ``plans/toy_examples.md``.

Both components are deliberately **stateless**, and should stay that way — they exist to be the
smallest readable example of each kind.  Cross-firing state *is* supported now, but it is a separate
idea with its own declaration (``HwModule.add_state``; see ``plans/add_state.md`` and
``tests/hw/test_add_state.py`` for the accumulator version).  Do not add an accumulator here.

For a real end-to-end example (build DAG, Vitis C-sim/co-sim, docs), see ``examples/regmap``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataArray, FloatField
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.hw.mem_stream import KernelTask
from waveflow.hw.synth import synthesizable
from waveflow.simulation.simobj import ProcessGen

Float32 = FloatField.specialize(bitwidth=32)


class Vec(DataArray):
    """The stream payload: an n-vector of Float32 (here n = 4)."""

    element_type = Float32
    static = True
    max_shape = (4,)


@dataclass
class Square(FreeRunMod):
    """y = x*x, element-wise over one Vec per firing."""

    cpp_kernel_name: ClassVar[str | None] = "square"

    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.x_in = StreamIFSlave(name=f"{self.name}_x_in", sim=self.sim, bitwidth=32)
        self.y_out = StreamIFMaster(name=f"{self.name}_y_out", sim=self.sim, bitwidth=32)
        self.add_endpoint(self.x_in)
        self.add_endpoint(self.y_out)

    def kernel_task(self) -> KernelTask:
        """Names the generated body so the ``ap_ctrl_none`` top can be derived from the graph.
        ``signature`` is the endpoint attribute names in task-argument order — the seam that makes
        the top's port list and the task's call args literally the same list."""
        return KernelTask("square_task", "square_task.h", ("x_in", "y_out"), template_args=())

    def run_iter(self) -> ProcessGen[None]:
        x = yield from self.x_in.get(Vec)      # one n-vector
        y = self.square(x)
        yield from self.y_out.write(y)

    @synthesizable
    def square(self, x: Vec) -> Vec:
        return x * x                           # element-wise y = x², the array-operator idiom


@dataclass
class Double(FreeRunMod):
    """z = x + x, element-wise over one Vec per firing."""

    # NOTE: not "double" — that is a C++ keyword, so it could never be a kernel function name.
    cpp_kernel_name: ClassVar[str | None] = "vec_double"

    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.x_in = StreamIFSlave(name=f"{self.name}_x_in", sim=self.sim, bitwidth=32)
        self.z_out = StreamIFMaster(name=f"{self.name}_z_out", sim=self.sim, bitwidth=32)
        self.add_endpoint(self.x_in)
        self.add_endpoint(self.z_out)

    def run_iter(self) -> ProcessGen[None]:
        x = yield from self.x_in.get(Vec)
        yield from self.z_out.write(self.dbl(x))

    @synthesizable
    def dbl(self, x: Vec) -> Vec:
        return x + x                           # z = 2·x


@dataclass
class ScaledSquare(FreeRunMod):
    """Composite: y = (2·x)², computed by two concurrent free-running sub-components."""

    cpp_kernel_name: ClassVar[str | None] = "scaled_square"

    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()

        # 1. sub-components — each runs its own run_iter loop concurrently
        self.double = Double(name=f"{self.name}_double", sim=self.sim, clk=self.clk)
        self.square = Square(name=f"{self.name}_square", sim=self.sim, clk=self.clk)
        self.add_comp(self.double)
        self.add_comp(self.square)

        # 2. internal edge — double.z_out (master) -> square.x_in (slave).
        #    StreamIF REQUIRES clk: it models transfer latency as nwords/clk.freq, and
        #    QueuedTransferIF.__post_init__ raises "clock must be provided" without it.
        self.z_if = StreamIF(name=f"{self.name}_z_if", sim=self.sim, clk=self.clk, bitwidth=32)
        self.z_if.bind("master", self.double.z_out)
        self.z_if.bind("slave", self.square.x_in)
        self.add_if(self.z_if)

        # 3. boundary — the composite's ports ARE its children's endpoints
        self.x_in = self.double.x_in      # composite input  = doubler's input
        self.y_out = self.square.y_out    # composite output = squarer's output
