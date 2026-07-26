"""Tests for :func:`codegen_path` — typed, class-based codegen dispatch (Phase 4).

The dispatch decision comes from the component's **class**, over the shared extraction engine — it
replaced the generic ``select_kernel_method`` string-resolver.
"""
from __future__ import annotations

from dataclasses import dataclass

from waveflow.build.codegen_dispatch import CodegenPath, codegen_path
from waveflow.build.elaborate import elaborate
from waveflow.hw.dataschema import IntField
from waveflow.hw.hw_module import HwModule
from waveflow.hw.hw_hostactivated import HostActivated
from waveflow.hw.regmap import RegAccess, RegField, VitisRegMap, VitisRegMapMMIFSlave
from waveflow.simulation.simobj import ProcessGen

_Int32 = IntField.specialize(bitwidth=32, signed=True)


def _add_regmap(self) -> None:
    """Attach a minimal VitisRegMapMMIFSlave to *self* (mirrors the example wiring)."""
    self.regmap = VitisRegMap({"y": RegField(_Int32, RegAccess.R)})
    self.s_lite = VitisRegMapMMIFSlave(
        name=f"{self.name}_s_lite", sim=self.sim, bitwidth=32,
        regmap=self.regmap, on_start=self.on_start)
    self.add_endpoint(self.s_lite)


def test_hostactivated_dispatches_to_on_start():
    from examples.stream_inband.poly import PolyAccel
    assert codegen_path(elaborate(PolyAccel)) == CodegenPath("leaf", "on_start")


def test_freeruncomp_dispatches_to_run_iter():
    from waveflow.hw.mem_stream import MemRStream
    assert codegen_path(elaborate(MemRStream)) == CodegenPath("leaf", "run_iter")


def test_compositecomp_dispatches_to_composite():
    from examples.mem_copy.mem_copy import MemCopy
    assert codegen_path(elaborate(MemCopy, {"mem_dwidth": 64})) == CodegenPath("composite", None)


def test_testbench_dispatches_to_main():
    from examples.stream_inband.poly import PolyTBHls
    assert codegen_path(elaborate(PolyTBHls)) == CodegenPath("testbench", "main")


def test_plain_hwcomponent_with_regmap_dispatches_to_on_start():
    """The interim fallback: a plain (un-migrated) HwModule carrying a regmap is host-activated."""
    @dataclass
    class _PlainRegmap(HwModule):
        def __post_init__(self):
            super().__post_init__()
            _add_regmap(self)

        def on_start(self) -> ProcessGen[None]:
            yield

    assert codegen_path(elaborate(_PlainRegmap)) == CodegenPath("leaf", "on_start")


def test_plain_free_running_hwcomponent_dispatches_to_run_proc():
    @dataclass
    class _Plain(HwModule):
        def run_proc(self) -> ProcessGen[None]:
            while True:
                yield

    assert codegen_path(elaborate(_Plain)) == CodegenPath("leaf", "run_proc")


def test_regmap_hwcomponent_dispatches_to_on_start_by_class():
    """A bare regmap-bearing HwModule (not FreeRunMod/HostActivated) is dispatched by the interim
    regmap fallback -> on_start; the class, not a _kernel_method string, decides."""
    @dataclass
    class _RegmapPlain(HwModule):
        def __post_init__(self):
            super().__post_init__()
            _add_regmap(self)

        def on_start(self) -> ProcessGen[None]:
            yield

    assert codegen_path(elaborate(_RegmapPlain)) == CodegenPath("leaf", "on_start")


def test_hostactivated_subclass_beats_regmap_fallback_ordering():
    """A HostActivated is matched by class before the generic regmap branch — proving the dispatch is
    class-typed, not endpoint-sniffing."""
    @dataclass
    class _HA(HostActivated):
        def __post_init__(self):
            super().__post_init__()
            _add_regmap(self)

        def on_start(self) -> ProcessGen[None]:
            yield

    assert codegen_path(elaborate(_HA)) == CodegenPath("leaf", "on_start")
