"""Tests for :func:`select_kernel_method` — the single kernel-entry selection
shared by ``extract_kernel`` (extraction) and the resolver (resolution scope)."""
from __future__ import annotations

from dataclasses import dataclass

from waveflow.build.elaborate import elaborate
from waveflow.build.hwresolve import select_kernel_method
from waveflow.hw.dataschema import IntField
from waveflow.hw.hw_component import HwComponent, SynthComp
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


def test_regmap_component_selects_on_start():
    from examples.stream_inband.poly import PolyAccelComponent
    assert select_kernel_method(elaborate(PolyAccelComponent)) == "on_start"


def test_regmap_synthcomp_no_explicit_selects_on_start():
    """The _kernel_method/regmap trap fix: a regmap-bearing SynthComp that relies
    on the (now None) default must resolve to on_start via the regmap fallback,
    not to the old 'run_proc' default."""
    @dataclass
    class _RegmapSynth(SynthComp):
        def __post_init__(self):
            super().__post_init__()
            _add_regmap(self)

        def on_start(self) -> ProcessGen[None]:
            yield

    assert select_kernel_method(elaborate(_RegmapSynth)) == "on_start"


def test_regmap_synthcomp_explicit_run_iter_wins():
    """An explicitly declared _kernel_method still beats the regmap fallback."""
    from typing import ClassVar

    @dataclass
    class _RegmapExplicit(SynthComp):
        _kernel_method: ClassVar[str] = "run_iter"

        def __post_init__(self):
            super().__post_init__()
            _add_regmap(self)

        def on_start(self) -> ProcessGen[None]:
            yield

        def run_iter(self) -> ProcessGen[None]:
            yield

    assert select_kernel_method(elaborate(_RegmapExplicit)) == "run_iter"


def test_freeruncomp_selects_run_iter():
    # Explicit _kernel_method='run_iter' wins over the regmap/run_proc fallback.
    from waveflow.hw.mem_stream import MemRStream
    assert select_kernel_method(elaborate(MemRStream)) == "run_iter"


def test_plain_free_running_component_selects_run_proc():
    @dataclass
    class _Plain(HwComponent):
        def run_proc(self):
            while True:
                yield

    assert select_kernel_method(elaborate(_Plain)) == "run_proc"


def test_testbench_selects_main():
    from examples.stream_inband.poly import PolyTBHls
    assert select_kernel_method(elaborate(PolyTBHls)) == "main"
