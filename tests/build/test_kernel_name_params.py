"""One kernel class at several widths: ``kernel_name_params`` names a top per parameter set.

``cpp_kernel_name`` is a class attribute, so two instances of one class at two widths would build
the same top and collide in one system or DAG.  A class that lists ``HwParam`` fields in
``kernel_name_params`` gets ``<cpp_kernel_name>_<param><value>`` per instance
(:func:`~waveflow.hw.hw_module.kernel_name`), everywhere the system flow names a top; a class that
lists none keeps its name -- which is every class that existed before, so no calibration record
or generated file moves.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import numpy as np
import pytest

from waveflow.build.build import BuildDag
from waveflow.build.composite_gen import composite_top_spec
from waveflow.build.system_dag import add_system_steps
from waveflow.hw.clock import Clock
from waveflow.hw.harness import harness
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import HwParam, kernel_name
from waveflow.hw.interface import FramedStreamIFMaster, FramedStreamIFSlave
from waveflow.hw.mem_stream import KernelTask
from waveflow.simulation.simulation import Simulation
from waveflow.utils.burst_io import StreamBurst


@dataclass
class _Scale(FreeRunMod):
    """A word in, a word out, at a build-time width."""

    cpp_kernel_name: ClassVar[str | None] = "wscale"
    kernel_name_params: ClassVar[tuple[str, ...]] = ("w",)

    w: HwParam[int] = 32
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.s_in = FramedStreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=int(self.w))
        self.m_out = FramedStreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=int(self.w))
        for ep in (self.s_in, self.m_out):
            self.add_endpoint(ep)

    def workload(self, kind: str = "test") -> dict:
        return {"s_in": [StreamBurst(np.arange(1, 5, dtype=np.uint64), True)]}

    def kernel_task(self) -> KernelTask:            # one templated body serves every width
        return KernelTask("wscale_task", "wscale_task.h", ("s_in", "m_out"),
                          template_args=(int(self.w),))

    def run_iter(self):
        words = yield from self.s_in.get(nwords_max=1)
        yield from self.m_out.write(words)


def _at(w: int) -> _Scale:
    return _Scale(name="k", sim=Simulation(), w=w)


def test_each_parameter_set_names_its_own_top():
    assert kernel_name(_at(32)) == "wscale_w32"
    assert kernel_name(_at(64)) == "wscale_w64"
    assert kernel_name(_Scale) == "wscale_w32"                 # a class: its defaults
    assert kernel_name(_Scale, {"w": 128}) == "wscale_w128"
    s32, s64 = (composite_top_spec(_at(w), width=w) for w in (32, 64))
    assert (s32.top_name, s64.top_name) == ("wscale_w32", "wscale_w64")
    assert {p.width for p in s32.ports} == {32} and {p.width for p in s64.ports} == {64}


def test_a_class_without_kernel_name_params_keeps_its_name():
    from examples.stream_kernel.stream_kernel import PolyKernel
    mod = PolyKernel(name="poly_kernel", sim=Simulation())
    assert kernel_name(mod) == "poly_kernel"
    assert composite_top_spec(mod, width=64).top_name == "poly_kernel"


def test_an_unknown_parameter_is_an_error():
    @dataclass
    class _Bad(_Scale):
        kernel_name_params: ClassVar[tuple[str, ...]] = ("width",)

    with pytest.raises(ValueError, match="not HwParam"):
        kernel_name(_Bad(name="b", sim=Simulation()))


def test_two_widths_coexist_on_one_dag():
    """Two harnesses of one class, at 32 and 64 bits, on one DAG: two tops, each csynth'd once."""
    dag = BuildDag()
    for w in (32, 64):
        sysm = harness(_at(w))
        assert sysm.dut().name == f"wscale_w{w}"
        add_system_steps(dag, sysm, work_dir="w", top=f"scale_top_{w}", prefix=f"w{w}_")
    tops = sorted(t for s in dag.steps() if "csynth" in s.name for t in s.tops)
    assert tops == ["wscale_w32", "wscale_w64"]
    assert {"csynth", "w64_csynth"} <= set(dag.step_names())
