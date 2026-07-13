"""Tests for :func:`select_kernel_method` — the single kernel-entry selection
shared by ``extract_kernel`` (extraction) and the resolver (resolution scope)."""
from __future__ import annotations

from dataclasses import dataclass

from waveflow.build.elaborate import elaborate
from waveflow.build.hwresolve import select_kernel_method
from waveflow.hw.hw_component import HwComponent


def test_regmap_component_selects_on_start():
    from examples.stream_inband.poly import PolyAccelComponent
    assert select_kernel_method(elaborate(PolyAccelComponent)) == "on_start"


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
