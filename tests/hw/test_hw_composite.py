"""Tests for :class:`CompositeComp` — now a thin subclass of :class:`FreeRunComp`.

A composite is no longer a separate kind; it is a ``FreeRunComp`` that has sub-components instead of a
``run_iter`` body (a leaf being the 1-task degenerate case of the same walk). ``CompositeComp`` survives
only to carry the ``composite_kernel`` target name. See ``plans/one_component_two_flows.md``.
"""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from waveflow.hw.codegen_targets import COMPOSITE_KERNEL, FREE_RUNNING_KERNEL
from waveflow.hw.hw_component import HwComponent
from waveflow.hw.hw_composite import CompositeComp
from waveflow.hw.hw_freerun import FreeRunComp
from waveflow.simulation.simulation import Simulation


def test_compositecomp_is_a_hwcomponent():
    assert issubclass(CompositeComp, HwComponent)


def test_compositecomp_is_now_a_freerun_subclass():
    """The merge is real: a composite IS a FreeRunComp. This replaces the old assertion that they were
    disjoint siblings — that split is exactly what one_component_two_flows removed."""
    assert issubclass(CompositeComp, FreeRunComp)


def test_compositecomp_only_adds_the_target_name():
    """The whole reason the subclass still exists: a composite lowers to composite_kernel, a leaf to
    free_running_kernel — two names for one product, collapsed in Stage 4. Everything else is
    inherited from FreeRunComp."""
    assert CompositeComp.potential_targets == frozenset({COMPOSITE_KERNEL})
    assert FreeRunComp.potential_targets == frozenset({FREE_RUNNING_KERNEL})


def test_a_composite_with_children_is_passive():
    """A composite (has sub-components, no body) returns None from run_proc — its children own the
    concurrent processes. The kind is decided by CONTENT, so the composite must actually have a child."""
    @dataclass
    class Leaf(FreeRunComp):
        def run_iter(self):  # noqa: ANN201
            yield self.timeout(1)

    @dataclass
    class Parent(CompositeComp):
        def __post_init__(self):
            super().__post_init__()
            self.add_comp(Leaf(name=f"{self.name}_child", sim=self.sim))

    parent = Parent(name="p", sim=Simulation())
    assert parent._kind() == "composite"
    assert parent.run_proc() is None


def test_body_xor_children_neither_fails_loudly():
    """An empty composite (no body, no children) is a user error, and now says so — where the old
    two-class world silently accepted it as a passive no-op."""
    @dataclass
    class Empty(CompositeComp):
        pass

    comp = Empty(name="c", sim=Simulation())
    with pytest.raises(TypeError, match="neither a run_iter body nor sub-components"):
        comp._kind()


def test_body_xor_children_both_fails_loudly():
    """A component that declares BOTH a body and children is ambiguous — refuse it. (This is the check
    the old CompositeComp did at class-definition time; it now runs post-construction, because children
    only exist after __post_init__.)"""
    @dataclass
    class GrandLeaf(FreeRunComp):
        def run_iter(self):  # noqa: ANN201
            yield self.timeout(1)

    @dataclass
    class Both(CompositeComp):
        def __post_init__(self):
            super().__post_init__()
            self.add_comp(GrandLeaf(name=f"{self.name}_child", sim=self.sim))

        def run_iter(self):  # noqa: ANN201
            yield self.timeout(1)

    comp = Both(name="b", sim=Simulation())
    with pytest.raises(TypeError, match="both a run_iter body and sub-components"):
        comp._kind()


def test_retrofit_composites_are_compositecomp():
    from examples.interleaver.interleaver import InterleaverCanon
    from examples.mem_copy.mem_copy import MemCopy
    assert issubclass(MemCopy, CompositeComp)
    assert issubclass(InterleaverCanon, CompositeComp)
    # And therefore FreeRunComps too — the point of the merge.
    assert issubclass(MemCopy, FreeRunComp)
