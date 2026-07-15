"""Tests for ``check(subject, target)`` — :mod:`waveflow.build.codegen_check`.

Two things are under test, and they are different in kind:

- **The gates** (1-3) are this module's own logic: is the target a real name, does it exist for this
  *kind*, is it implemented?  Cheap, and answered without touching a component's body.
- **Gate 4** is *not* this module's logic at all — it runs the real extractor.  So the tests below do
  not check that ``check`` knows the rules (it must not); they check that it faithfully **relays**
  them.  The crafted violations are the evidence: each asserts the message names the actual problem,
  and each of those messages is produced by :mod:`waveflow.build.hwcodegen`, not here.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import pytest

from waveflow.build.codegen_check import check, potential_targets
from waveflow.build.elaborate import elaborate
from waveflow.hw.clock import Clock
from waveflow.hw.codegen_targets import (
    ALL_TARGETS,
    COMPOSITE_KERNEL,
    CONCURRENT_SYSTEMC_TB,
    CONTROL_DRIVEN_KERNEL,
    FREE_RUNNING_KERNEL,
    IMPLEMENTED_TARGETS,
    SEQUENTIAL_VITIS_TB,
)
from waveflow.hw.dataschema import IntField
from waveflow.hw.hw_hostactivated import HostActivated
from waveflow.hw.regmap import RegAccess, RegField, VitisRegMap, VitisRegMapMMIFSlave
from waveflow.hw.synth import synthesizable
from waveflow.simulation.simobj import ProcessGen

from examples.block_scale.block_scale import BlockScaleTBHls
from examples.regmap.simp_fun import SimpFunComponent, SimpFunTBHls
from examples.shared_mem.hist import HistTBHls
from examples.stream_inband.poly import PolyTBHls
from examples.toy.toy import ScaledSquare, Square

Int32 = IntField.specialize(bitwidth=32, signed=True)


# ==============================================================================================
# Local fixtures — the smallest host-activated component that really lowers.
#
# `simp_fun`'s shape, minus everything not load-bearing here: a regmap boundary (`x` in, `y` out) and
# an `on_start` that reads it, calls a @synthesizable hook, and writes the result back.  Subclasses
# below vary ONE thing each so the verdict has a single cause.
# ==============================================================================================

def _add_xy_regmap(comp) -> None:
    """The minimal host-activated boundary: one input `x`, one output `y`, `ap_start`/`ap_done`."""
    comp.regmap = VitisRegMap({
        "x": RegField(Int32, RegAccess.RW, description="Input operand"),
        "y": RegField(Int32, RegAccess.R, description="Result"),
    })
    comp.regmap.set("y", 0)
    comp.s_lite = VitisRegMapMMIFSlave(
        name=f"{comp.name}_s_lite", sim=comp.sim, bitwidth=32,
        regmap=comp.regmap, on_start=comp.on_start,
    )
    comp.add_endpoint(comp.s_lite)


@dataclass
class _MinimalHostActivated(HostActivated):
    """A host-activated leaf that passes `check` — the baseline the violations below deviate from."""

    cpp_kernel_name: ClassVar[str | None] = "minimal"
    cpp_namespace: ClassVar[str | None] = "minimal_impl"

    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        _add_xy_regmap(self)

    def on_start(self) -> ProcessGen[None]:
        # `self.timeout` is @sim_only, so the extractor strips this yield (and its whole argument
        # subtree) — it models latency in pysim and emits nothing.  Same spelling as `simp_fun`.
        yield self.timeout(self.clk.period)
        y = self.bump(self.regmap.get("x"))
        self.regmap.set("y", y)

    @synthesizable
    def bump(self, x: Int32) -> Int32:
        return Int32(int(x.val) + 1)


def test_the_local_baseline_fixture_really_passes():
    """Guard the fixtures: the violations below are only meaningful if the shape they start from
    lowers cleanly.  Otherwise a crafted test could pass for the wrong reason."""
    assert check(_MinimalHostActivated, CONTROL_DRIVEN_KERNEL) == (True, None)


# ==============================================================================================
# The real subjects — everything that lowers today must say so
# ==============================================================================================

@pytest.mark.parametrize(
    "subject",
    [SimpFunComponent, SimpFunTBHls, PolyTBHls, HistTBHls, BlockScaleTBHls],
    ids=lambda c: c.__name__,
)
def test_real_subjects_pass(subject):
    """Every subject codegen actually emits today passes its (only) potential target.

    This is the anti-shadow gate: these all run through Vitis, so a `False` here would mean `check`
    invented a rule the extractor does not have.
    """
    assert check(subject) == (True, None)


@pytest.mark.parametrize(
    "subject, target",
    [
        (SimpFunComponent, CONTROL_DRIVEN_KERNEL),
        (SimpFunTBHls, SEQUENTIAL_VITIS_TB),
        (PolyTBHls, SEQUENTIAL_VITIS_TB),
        (HistTBHls, SEQUENTIAL_VITIS_TB),
        (BlockScaleTBHls, SEQUENTIAL_VITIS_TB),
    ],
    ids=lambda x: getattr(x, "__name__", x),
)
def test_naming_the_target_explicitly_agrees_with_the_default(subject, target):
    """`target=None` resolves to the kind's single potential target — same verdict either way."""
    assert check(subject, target) == (True, None)
    assert check(subject, target) == check(subject)


def test_subject_may_be_a_class_or_an_instance():
    """`check(SimpFunComponent)` and `check(<a SimpFunComponent>)` ask the same question.

    A class is elaborated internally; an instance is used as-is (it is already elaborated).
    """
    assert check(SimpFunComponent) == (True, None)
    assert check(elaborate(SimpFunComponent)) == (True, None)
    assert check(SimpFunTBHls) == (True, None)
    assert check(elaborate(SimpFunTBHls)) == (True, None)


def test_potential_targets_reads_the_classvar_for_class_and_instance():
    assert potential_targets(SimpFunComponent) == frozenset({CONTROL_DRIVEN_KERNEL})
    assert potential_targets(elaborate(SimpFunComponent)) == frozenset({CONTROL_DRIVEN_KERNEL})
    assert potential_targets(SimpFunTBHls) == frozenset({SEQUENTIAL_VITIS_TB})
    assert potential_targets(Square) == frozenset({FREE_RUNNING_KERNEL})
    assert potential_targets(ScaledSquare) == frozenset({COMPOSITE_KERNEL})


# ==============================================================================================
# Gate 1 — unknown target name
# ==============================================================================================

def test_gate1_unknown_target_is_rejected_and_lists_the_known_names():
    ok, msg = check(SimpFunComponent, "vitis_kernel")   # a plausible-but-wrong name
    assert ok is False
    assert "Unknown codegen target 'vitis_kernel'" in msg
    for name in ALL_TARGETS:
        assert repr(name) in msg, "the message should list every known target"


def test_gate1_fires_before_the_kind_check():
    """An unknown name is a typo, not a 'wrong kind' — the message must say so."""
    ok, msg = check(SimpFunTBHls, "not_a_target_at_all")
    assert ok is False
    assert "Unknown codegen target" in msg
    assert "not a potential target" not in msg


# ==============================================================================================
# Gate 2 — a real target, but not one that exists for this kind
#
# This is the case the target axis exists to answer.
# ==============================================================================================

def test_gate2_target_not_potential_for_this_kind():
    ok, msg = check(SimpFunComponent, CONCURRENT_SYSTEMC_TB)
    assert ok is False
    assert "not a potential target" in msg
    assert CONCURRENT_SYSTEMC_TB in msg
    assert "SimpFunComponent" in msg
    assert CONTROL_DRIVEN_KERNEL in msg, "the message should name what IS potential"


def test_gate2_a_dut_target_is_not_a_tb_target():
    """The kinds are not interchangeable in either direction."""
    ok, msg = check(SimpFunTBHls, CONTROL_DRIVEN_KERNEL)
    assert ok is False and "not a potential target" in msg

    ok, msg = check(SimpFunComponent, SEQUENTIAL_VITIS_TB)
    assert ok is False and "not a potential target" in msg


def test_gate2_beats_gate3_an_unimplemented_target_of_another_kind():
    """`composite_kernel` is unimplemented AND not Square's kind — the kind answer is the useful one."""
    ok, msg = check(Square, COMPOSITE_KERNEL)
    assert ok is False
    assert "not a potential target" in msg


# ==============================================================================================
# Gate 3 — declared, but not implemented
# ==============================================================================================

def test_gate3_free_running_kernel_is_declared_but_unimplemented():
    ok, msg = check(Square, FREE_RUNNING_KERNEL)
    assert ok is False
    assert "not implemented yet" in msg
    assert FREE_RUNNING_KERNEL in msg
    assert "docs/guide/flows" in msg, "the message should point at the flow that will build it"


def test_gate3_composite_kernel_is_declared_but_unimplemented():
    ok, msg = check(ScaledSquare)     # its only potential target
    assert ok is False
    assert "not implemented yet" in msg
    assert COMPOSITE_KERNEL in msg


def test_check_and_generate_disagree_about_square_on_purpose():
    """`kernel_files_to_str(Square)` SUCCEEDS while `check(Square, "free_running_kernel")` says NO.

    This looks like a contradiction and is instead the entire point of the family.

    `generate` is not answering the question `check` asks.  Handed a `FreeRunComp`, codegen does not
    say "I cannot emit a free-running task" — it silently emits an `ap_ctrl_hs` top with an
    unextracted hook body (and an ill-formed namespace), i.e. *a different target than the one the
    component declares*, with no exception.  `check` is what tells the truth `generate` does not:
    `free_running_kernel` is not implemented, so nothing can lower to it today.

    `tests/examples/test_toy.py::test_square_codegen_is_not_yet_a_free_running_task` pins that
    broken emission in detail.  When the free-running path lands, BOTH tests must change together —
    which is the alarm this test exists to be.
    """
    from waveflow.build.hwgen import kernel_files_to_str

    assert kernel_files_to_str(Square)                       # generate: fine
    ok, msg = check(Square, FREE_RUNNING_KERNEL)             # check: no
    assert ok is False and "not implemented yet" in msg


# ==============================================================================================
# target=None — only defaultable when there is exactly one potential target
# ==============================================================================================

@dataclass
class _NoDeclaredTargets(_MinimalHostActivated):
    """A kind that declares no targets — stand-in for a subject off the execution-model classes."""

    cpp_kernel_name: ClassVar[str | None] = "no_targets"
    cpp_namespace: ClassVar[str | None] = "no_targets_impl"
    potential_targets: ClassVar[frozenset[str]] = frozenset()


@dataclass
class _TwoTargets(_MinimalHostActivated):
    """A kind with an ambiguous target set — the caller must say which."""

    cpp_kernel_name: ClassVar[str | None] = "two_targets"
    cpp_namespace: ClassVar[str | None] = "two_targets_impl"
    potential_targets: ClassVar[frozenset[str]] = frozenset(
        {CONTROL_DRIVEN_KERNEL, FREE_RUNNING_KERNEL}
    )


def test_target_none_needs_exactly_one_potential_target():
    ok, msg = check(_NoDeclaredTargets)
    assert ok is False
    assert "not been migrated to an execution-model class" in msg

    ok, msg = check(_TwoTargets)
    assert ok is False
    assert "several potential targets" in msg

    # ...but naming one resolves it.
    assert check(_TwoTargets, CONTROL_DRIVEN_KERNEL) == (True, None)


def test_unmigrated_plain_hwcomponent_says_so_and_does_not_send_you_in_a_circle():
    """The real un-migrated kernels: check() cannot answer, and must say why *usefully*.

    HistAccel and BlockScaleComponent are plain HwComponents with a run_proc body — the "interim
    un-migrated leaf" that codegen_dispatch explicitly still handles.  So `kernel_files_to_str`
    WORKS for them while `check` cannot answer: two of the four real kernels.

    The trap this pins: with an empty potential_targets, *no* target name can pass gate 2, so a
    message saying "name one explicitly" is a dead end — the caller tries a name, gets refused, and
    learns nothing.  The message must instead name the migration, and must not claim the component
    won't generate (the caller can watch it generate).
    """
    from examples.block_scale.block_scale import BlockScaleComponent
    from examples.shared_mem.hist import HistAccel

    for cls in (HistAccel, BlockScaleComponent):
        for target in (None, CONTROL_DRIVEN_KERNEL, FREE_RUNNING_KERNEL):
            ok, msg = check(cls, target)
            assert ok is False
            assert "not been migrated to an execution-model class" in msg
            assert "name one explicitly" not in msg, "dead-end advice: no name can pass gate 2"
            # It must not deny what the caller can see codegen do.
            assert "NOT a claim that it will not generate" in msg

    # The disagreement is real, not hypothetical: generate succeeds where check abstains.
    from waveflow.build.hwgen import kernel_files_to_str

    assert kernel_files_to_str(HistAccel), "HistAccel migrated? then update this test and the message"


# ==============================================================================================
# Gate 4 — the rules.  Crafted violations; each message must name the actual problem.
#
# NOTE: these all target `control_driven_kernel`, the only implemented DUT target — a violation
# behind an unimplemented target would never reach gate 4 to be seen.
# ==============================================================================================

@dataclass
class _CapturesMutableState(_MinimalHostActivated):
    """Violation (a): `on_start` reads mutable `self.gain` — an implicit capture.

    `gain` is a plain instance field: hardware state the kernel would have to carry, invisible at the
    boundary.  The extractor's allow-list (endpoints / RegMap / AXIMMQueue / HwParamValue / DataSchema
    types / @sim_only / @synthesizable) does not cover it, so the read is rejected.  The fix the
    message names — pass it explicitly, or mark it @sim_only — is what `simp_fun` does.

    The ONLY deviation from `_MinimalHostActivated` is the `self.gain` read.
    """

    cpp_kernel_name: ClassVar[str | None] = "cap_state"
    cpp_namespace: ClassVar[str | None] = "cap_state_impl"

    gain: int = 3

    def on_start(self) -> ProcessGen[None]:
        yield self.timeout(self.clk.period)
        y = self.scale(self.regmap.get("x"), self.gain)   # <-- self.gain: implicit capture
        self.regmap.set("y", y)

    @synthesizable
    def scale(self, x: Int32, g: int) -> Int32:
        return Int32(int(x.val) * int(g))


@dataclass
class _CallsNonSynthesizableMethod(_MinimalHostActivated):
    """Violation (b): `on_start` calls `self.helper`, which is neither @synthesizable nor @sim_only.

    An unmarked method has no C++ lowering and no hook boundary — codegen has nothing to emit for the
    call, so it is rejected rather than silently dropped.

    The ONLY deviation from `_MinimalHostActivated` is that the hook lost its marker.
    """

    cpp_kernel_name: ClassVar[str | None] = "plain_call"
    cpp_namespace: ClassVar[str | None] = "plain_call_impl"

    def on_start(self) -> ProcessGen[None]:
        yield self.timeout(self.clk.period)
        y = self.helper(self.regmap.get("x"))             # <-- unmarked method
        self.regmap.set("y", y)

    def helper(self, x: Int32) -> Int32:                  # no @synthesizable / @sim_only
        return Int32(int(x.val) + 1)


def test_gate4_implicit_capture_of_mutable_self_state():
    ok, msg = check(_CapturesMutableState, CONTROL_DRIVEN_KERNEL)
    assert ok is False
    assert "Implicit capture" in msg
    assert "self.gain" in msg, "the message must name the offending attribute"


def test_gate4_call_to_a_non_synthesizable_method():
    ok, msg = check(_CallsNonSynthesizableMethod, CONTROL_DRIVEN_KERNEL)
    assert ok is False
    assert "non-synthesizable method" in msg
    assert "helper" in msg, "the message must name the offending method"


def test_gate4_relays_the_extractor_verbatim():
    """`check`'s gate-4 message IS the extractor's message — not a paraphrase of it.

    If this ever fails, someone has started re-wording (or worse, re-deriving) the rules here, which
    is the "shadow" the design forbids: a second copy that drifts from what codegen accepts.
    """
    from waveflow.build.hwcodegen import SynthesisError, extract_kernel

    comp = elaborate(_CapturesMutableState)
    with pytest.raises(SynthesisError) as excinfo:
        extract_kernel(comp)

    ok, msg = check(_CapturesMutableState, CONTROL_DRIVEN_KERNEL)
    assert ok is False
    assert msg == str(excinfo.value)


def test_gate4_does_not_swallow_non_synthesis_errors():
    """Only `SynthesisError` is a verdict.  Any other exception is a bug and must propagate.

    Returning `(False, ...)` for, say, a `TypeError` in `__post_init__` would report "not
    synthesizable" for a component that is merely broken — and eat the traceback that explains it.
    """
    @dataclass
    class _ExplodesOnElaboration(_MinimalHostActivated):
        cpp_kernel_name: ClassVar[str | None] = "boom"
        cpp_namespace: ClassVar[str | None] = "boom_impl"

        def __post_init__(self) -> None:
            super().__post_init__()
            raise RuntimeError("elaboration is broken, this is not a synthesis verdict")

    with pytest.raises(RuntimeError, match="elaboration is broken"):
        check(_ExplodesOnElaboration, CONTROL_DRIVEN_KERNEL)


# ==============================================================================================
# The vocabulary
# ==============================================================================================

def test_implemented_targets_is_a_subset_of_all_targets():
    assert IMPLEMENTED_TARGETS <= ALL_TARGETS


def test_every_declared_potential_target_is_a_known_name():
    """A kind cannot declare a target the vocabulary does not know."""
    for kind in (SimpFunComponent, Square, ScaledSquare, SimpFunTBHls):
        assert potential_targets(kind) <= ALL_TARGETS, kind.__name__
