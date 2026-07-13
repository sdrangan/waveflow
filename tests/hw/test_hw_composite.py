"""Tests for :class:`CompositeComp` — the hierarchical (bodyless) synthesizable component."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from waveflow.hw.hw_component import ControlMode, HwComponent
from waveflow.hw.hw_composite import CompositeComp
from waveflow.simulation.simulation import Simulation


def test_compositecomp_is_a_hwcomponent():
    assert issubclass(CompositeComp, HwComponent)


def test_compositecomp_is_not_freerun():
    # A composite is a passive sibling of FreeRunComp, not a subclass of it.
    from waveflow.hw.hw_freerun import FreeRunComp
    assert not issubclass(CompositeComp, FreeRunComp)
    assert not issubclass(FreeRunComp, CompositeComp)


def test_compositecomp_is_passive():
    """run_proc stays the SimObj default (None) — no process scheduled."""
    @dataclass
    class Empty(CompositeComp):
        pass

    comp = Empty(name="c", sim=Simulation())
    assert comp.run_proc() is None


def test_compositecomp_control_mode_left_auto():
    """Execution model follows the boundary; the class must not hardcode it."""
    assert CompositeComp.control_mode == ControlMode.AUTO


def test_compositecomp_rejects_run_iter():
    """A composite has no kernel body — defining run_iter fails loudly at class definition."""
    with pytest.raises(TypeError, match="defines run_iter"):
        @dataclass
        class Bad(CompositeComp):
            def run_iter(self):  # noqa: ANN201
                yield


def test_retrofit_composites_are_compositecomp():
    from examples.interleaver.interleaver import InterleaverCanon
    from examples.interleaver.mem_copy import MemCopy
    assert issubclass(MemCopy, CompositeComp)
    assert issubclass(InterleaverCanon, CompositeComp)
