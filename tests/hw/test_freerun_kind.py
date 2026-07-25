"""Tests for :class:`FreeRunComp`'s **standalone vs composite** kind.

A composite is not a separate class; it is a ``FreeRunComp`` that has sub-components instead of a
``run_iter`` body (a standalone component being the 1-task degenerate case of the same walk). The kind
is decided by CONTENT, post-construction, and both shapes lower to ``composite_kernel`` through one
generator. See ``plans/one_component_two_flows.md``.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from waveflow.hw.codegen_targets import COMPOSITE_KERNEL
from waveflow.hw.hw_component import HwComponent
from waveflow.hw.hw_freerun import FreeRunComp
from waveflow.simulation.simulation import Simulation


def test_freeruncomp_is_a_hwcomponent():
    assert issubclass(FreeRunComp, HwComponent)


def test_standalone_and_composite_share_one_target():
    """Both lower to composite_kernel — a standalone component is the 1-task degenerate case, one
    product, one name.  There is no separate composite class or target to route to."""
    assert FreeRunComp.potential_targets == frozenset({COMPOSITE_KERNEL})


def test_a_composite_with_children_is_passive():
    """A composite (has sub-components, no body) returns None from run_proc — its children own the
    concurrent processes. The kind is decided by CONTENT, so the composite must actually have a child."""
    @dataclass
    class Standalone(FreeRunComp):
        def run_iter(self):  # noqa: ANN201
            yield self.timeout(1)

    @dataclass
    class Parent(FreeRunComp):
        def __post_init__(self):
            super().__post_init__()
            self.add_comp(Standalone(name=f"{self.name}_child", sim=self.sim))

    parent = Parent(name="p", sim=Simulation())
    assert parent._kind() == "composite"
    assert parent.run_proc() is None


def test_a_standalone_with_a_body_runs():
    """A standalone component (overrides run_iter, no children) reports its kind and drives a loop."""
    @dataclass
    class Standalone(FreeRunComp):
        def run_iter(self):  # noqa: ANN201
            yield self.timeout(1)

    comp = Standalone(name="s", sim=Simulation())
    assert comp._kind() == "standalone"
    assert comp.run_proc() is not None


def test_body_xor_children_neither_fails_loudly():
    """A component with no body and no children is a user error, and now says so — where the old
    two-class world silently accepted it as a passive no-op."""
    @dataclass
    class Empty(FreeRunComp):
        pass

    comp = Empty(name="c", sim=Simulation())
    with pytest.raises(TypeError, match="neither a run_iter body nor sub-components"):
        comp._kind()


def test_body_xor_children_both_fails_loudly():
    """A component that declares BOTH a body and children is ambiguous — refuse it. (This runs
    post-construction, because children only exist after __post_init__.)"""
    @dataclass
    class GrandStandalone(FreeRunComp):
        def run_iter(self):  # noqa: ANN201
            yield self.timeout(1)

    @dataclass
    class Both(FreeRunComp):
        def __post_init__(self):
            super().__post_init__()
            self.add_comp(GrandStandalone(name=f"{self.name}_child", sim=self.sim))

        def run_iter(self):  # noqa: ANN201
            yield self.timeout(1)

    comp = Both(name="b", sim=Simulation())
    with pytest.raises(TypeError, match="both a run_iter body and sub-components"):
        comp._kind()


def test_retrofit_composites_are_freeruncomps():
    from examples.interleaver.interleaver_inband import InterleaverInband
    from examples.mem_copy.mem_copy import MemCopy
    assert issubclass(MemCopy, FreeRunComp)
    assert issubclass(InterleaverInband, FreeRunComp)
