"""Tests for ``check(source, target)`` — :mod:`waveflow.build.codegen_check`.

Two things are under test, and they are different in kind:

- **The gates** (1-3) are this module's own logic: is the target a real name, does it exist for this
  *kind*, is it implemented?  Cheap, and answered without touching a component's body.
- **Gate 4** is *not* this module's logic at all — it runs the real extractor.  So the tests below do
  not check that ``check`` knows the rules (it must not); they check that it faithfully **relays**
  them.  The crafted violations are the evidence: each asserts the message names the actual problem,
  and each of those messages is produced by :mod:`waveflow.build.hwcodegen`, not here.
"""
from __future__ import annotations

import ast
import textwrap
from dataclasses import dataclass, field
from typing import ClassVar

import pytest

from waveflow.build.codegen_check import check, potential_targets
from waveflow.build.elaborate import elaborate
from waveflow.hw.clock import Clock
from waveflow.hw.codegen_targets import (
    ALL_TARGETS,
    BITSTREAM,
    COMPOSITE_KERNEL,
    CONTROL_DRIVEN_KERNEL,
    IMPLEMENTED_TARGETS,
    SEQUENTIAL_VITIS_TB,
    SEQUENTIAL_XSI_TB,
)
from waveflow.hw.dataschema import IntField
from waveflow.hw.hw_hostactivated import HostActivated
from waveflow.hw.hw_testbench import SeqTB
from waveflow.hw.regmap import RegAccess, RegField, VitisRegMap, VitisRegMapMMIFSlave
from waveflow.hw.synth import synthesizable
from waveflow.simulation.simobj import ProcessGen

from examples.block_scale.block_scale import BlockScaleTBHls
from examples.regmap.simp_fun import SimpFunComponent, SimpFunTBHls
from examples.shared_mem.hist import HistAccel, HistTBHls
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
# The real sources — everything that lowers today must say so
# ==============================================================================================

@pytest.mark.parametrize(
    "source",
    [SimpFunComponent, HistAccel, SimpFunTBHls, PolyTBHls, HistTBHls, BlockScaleTBHls],
    ids=lambda c: c.__name__,
)
def test_real_sources_pass(source):
    """Every source codegen actually emits today passes its (only) potential target.

    This is the anti-shadow gate: these all run through Vitis, so a `False` here would mean `check`
    invented a rule the extractor does not have.
    """
    assert check(source) == (True, None)


@pytest.mark.parametrize(
    "source, target",
    [
        (SimpFunComponent, CONTROL_DRIVEN_KERNEL),
        (HistAccel, CONTROL_DRIVEN_KERNEL),
        (SimpFunTBHls, SEQUENTIAL_VITIS_TB),
        (PolyTBHls, SEQUENTIAL_VITIS_TB),
        (HistTBHls, SEQUENTIAL_VITIS_TB),
        (BlockScaleTBHls, SEQUENTIAL_VITIS_TB),
    ],
    ids=lambda x: getattr(x, "__name__", x),
)
def test_naming_the_target_explicitly_agrees_with_the_default(source, target):
    """`target=None` resolves to the kind's single potential target — same verdict either way."""
    assert check(source, target) == (True, None)
    assert check(source, target) == check(source)


def test_source_may_be_a_class_or_an_instance():
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
    # A leaf and a composite now share one target name — the merge collapsed free_running_kernel into
    # composite_kernel (a leaf is the 1-task case). See plans/one_component_two_flows.md.
    assert potential_targets(Square) == frozenset({COMPOSITE_KERNEL})
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
    # A host-activated DUT cannot lower to the XSI-TB target — a real name, wrong kind.
    ok, msg = check(SimpFunComponent, SEQUENTIAL_XSI_TB)
    assert ok is False
    assert "not a potential target" in msg
    assert SEQUENTIAL_XSI_TB in msg
    assert "SimpFunComponent" in msg
    assert CONTROL_DRIVEN_KERNEL in msg, "the message should name what IS potential"


def test_gate2_a_dut_target_is_not_a_tb_target():
    """The kinds are not interchangeable in either direction."""
    ok, msg = check(SimpFunTBHls, CONTROL_DRIVEN_KERNEL)
    assert ok is False and "not a potential target" in msg

    ok, msg = check(SimpFunComponent, SEQUENTIAL_VITIS_TB)
    assert ok is False and "not a potential target" in msg


def test_gate2_beats_gate3_an_unimplemented_target_of_another_kind():
    """`bitstream` is unimplemented AND not Square's kind — the kind answer is the useful one.

    (Was `composite_kernel`, which is now both Square's kind and implemented, so `bitstream` — the one
    remaining unimplemented target, and one no source declares — plays the role.)"""
    ok, msg = check(Square, BITSTREAM)
    assert ok is False
    assert "not a potential target" in msg


# ==============================================================================================
# Gate 3 — declared, but not implemented
#
# After the Flow-2 collapse, the ONLY unimplemented target is `bitstream`, and no real source
# declares it — so gate 3 is exercised through a synthetic kind that does.
# ==============================================================================================

@dataclass
class _BitstreamKind(_MinimalHostActivated):
    """A kind whose only potential target is the (still-unimplemented) `bitstream` — the last live
    case for gate 3 now that Flows 1 and 2 are built."""

    cpp_kernel_name: ClassVar[str | None] = "bitstream_kind"
    cpp_namespace: ClassVar[str | None] = "bitstream_kind_impl"
    potential_targets: ClassVar[frozenset[str]] = frozenset({BITSTREAM})


def test_gate3_bitstream_is_declared_but_unimplemented():
    ok, msg = check(_BitstreamKind, BITSTREAM)
    assert ok is False
    assert "not implemented yet" in msg
    assert BITSTREAM in msg
    assert "docs/guide/flows" in msg, "the message should point at the flow that will build it"


def test_flow2_targets_are_now_implemented():
    """The point of the collapse: composite_kernel + sequential_xsi_tb are BUILT, so they pass gate 3
    and a well-formed Flow-2 component/testbench reaches gate 4 and validates via the real generator.

    This is the pair to the old `test_gate3_*_is_declared_but_unimplemented` tests, inverted: what was
    "declared but not built" is now built. See plans/one_component_two_flows.md."""
    from examples.mem_copy.mem_copy import MemCopy
    from examples.mem_copy.mem_copy_sim import MemCopyTB

    assert {COMPOSITE_KERNEL, SEQUENTIAL_XSI_TB} <= IMPLEMENTED_TARGETS
    assert check(MemCopy, COMPOSITE_KERNEL) == (True, None)          # composite DUT, real generator
    assert check(MemCopyTB, SEQUENTIAL_XSI_TB) == (True, None)       # its XSI testbench


def test_check_runs_the_real_generator_and_an_unwired_toy_still_cannot_lower():
    """`kernel_files_to_str(Square)` SUCCEEDS, but `check(Square, "composite_kernel")` does NOT pass —
    the point of the check family, updated for the collapse.

    `generate` does not answer check's question: handed the `Square` toy it silently emits an
    `ap_ctrl_hs` top with an unextracted hook body — *a different target than the one declared* — with
    no exception (pinned in detail by test_toy.py::test_square_codegen_is_not_yet_a_free_running_task).

    Now that `composite_kernel` is implemented, gate 4 runs the REAL generator (`composite_top_spec`)
    for it. `Square` declares the target but never wired the leaf machinery (no `kernel_task`), so the
    walk fails. INTERIM: the graph walk has no verdict exception yet, so this surfaces as a raised
    `AttributeError` rather than a clean `(False, msg)` — the exception-taxonomy follow-up in
    plans/one_component_two_flows.md is what turns it into a verdict. When it lands, this assertion
    changes from `pytest.raises` to `(False, ...)`, and both tests move together."""
    from waveflow.build.hwgen import kernel_files_to_str

    assert kernel_files_to_str(Square)                       # generate: fine (wrong target, silently)
    with pytest.raises(AttributeError, match="kernel_task"):  # check: runs the real generator, fails
        check(Square, COMPOSITE_KERNEL)


# ==============================================================================================
# target=None — only defaultable when there is exactly one potential target
# ==============================================================================================

@dataclass
class _NoDeclaredTargets(_MinimalHostActivated):
    """A kind that declares no targets — stand-in for a source off the execution-model classes."""

    cpp_kernel_name: ClassVar[str | None] = "no_targets"
    cpp_namespace: ClassVar[str | None] = "no_targets_impl"
    potential_targets: ClassVar[frozenset[str]] = frozenset()


@dataclass
class _TwoTargets(_MinimalHostActivated):
    """A kind with an ambiguous target set — the caller must say which."""

    cpp_kernel_name: ClassVar[str | None] = "two_targets"
    cpp_namespace: ClassVar[str | None] = "two_targets_impl"
    potential_targets: ClassVar[frozenset[str]] = frozenset(
        {CONTROL_DRIVEN_KERNEL, COMPOSITE_KERNEL}
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
    """The real un-migrated kernel: check() cannot answer, and must say why *usefully*.

    BlockScaleComponent is a plain HwComponent with a run_proc body — the "interim un-migrated
    leaf" that codegen_dispatch explicitly still handles.  So `kernel_files_to_str` WORKS for it
    while `check` cannot answer: one of the four real kernels.

    (HistAccel used to be the other one.  It is now a HostActivated — see
    `test_hist_is_migrated_and_checkable` below — which is why this covers BlockScaleComponent
    alone.  When block_scale migrates too, this test has no source left and should be deleted
    along with `_no_targets_message`'s HwComponent branch; `_NoDeclaredTargets` above already
    covers the message shape synthetically.)

    The trap this pins: with an empty potential_targets, *no* target name can pass gate 2, so a
    message saying "name one explicitly" is a dead end — the caller tries a name, gets refused, and
    learns nothing.  The message must instead name the migration, and must not claim the component
    won't generate (the caller can watch it generate).
    """
    from examples.block_scale.block_scale import BlockScaleComponent

    for target in (None, CONTROL_DRIVEN_KERNEL, COMPOSITE_KERNEL):
        ok, msg = check(BlockScaleComponent, target)
        assert ok is False
        assert "not been migrated to an execution-model class" in msg
        assert "name one explicitly" not in msg, "dead-end advice: no name can pass gate 2"
        # It must not deny what the caller can see codegen do.
        assert "NOT a claim that it will not generate" in msg

    # The disagreement is real, not hypothetical: generate succeeds where check abstains.
    from waveflow.build.hwgen import kernel_files_to_str

    assert kernel_files_to_str(BlockScaleComponent), \
        "BlockScaleComponent migrated? then update this test and the message"


def test_hist_is_migrated_and_checkable():
    """HistAccel is a HostActivated: `check` now returns a real verdict where it used to abstain.

    This is the structural half of the Stage-4 contract, and the pair to the test above.  hist
    keeps its command **in-band on s_in** — only the control plane moved onto the regmap.  The
    regmap is control-only (`VitisRegMap({})`, no application registers), which is what fills the
    `0x00 : reserved` slot in the AXI-Lite slave that `m_axi ... offset=slave` already forced into
    existence for `m_mem` at `0x10`.
    """
    from examples.shared_mem.hist import HistAccel

    assert issubclass(HistAccel, HostActivated)
    assert potential_targets(HistAccel) == frozenset({CONTROL_DRIVEN_KERNEL})
    assert check(HistAccel) == (True, None)
    assert check(HistAccel, CONTROL_DRIVEN_KERNEL) == (True, None)

    # The control plane is the ONLY thing that moved: no application registers were added, so the
    # kernel signature is unchanged and the command still rides in-band on s_in.
    comp = elaborate(HistAccel)
    assert [n for n, f in comp.regmap._fields.items() if not f.is_vitis_auto] == []


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
# Gate 4 — the sequential gate (Stage 3), the family's only NEW rule.
#
# It lives in the extractor (`HwStmtExtractor._validate_no_concurrency`), not here — so `check`
# reports it for free and `generate` enforces it too.  A rule added to `codegen_check.py` instead
# would be the "shadow" this module's docstring forbids.
#
# It is a GATE, NOT A PROOF: it rejects a syntactic construct that certainly implies concurrency;
# it does not certify that a body which passes is sequential.  The tests below are written to that
# claim and no stronger one.
# ==============================================================================================

class _SequentialTB(SeqTB):
    """A straight-line TB that passes `check` — the baseline the spawn below deviates from.

    `simp_fun`'s TB shape: read the operands from disk, drive one timed invocation inline via
    `yield from`.  (`yield from ()` is NOT an extractable body shape — gate 4 rejects it — so the
    fixtures mirror the real `yield from dut.run_once_sim(...)` spelling.)
    """

    cpp_kernel_name: ClassVar[str | None] = "simp_fun"

    def main(self):
        dut = SimpFunComponent()
        x = Int32().read_uint32_file(self.data_dir + "/x.bin")
        a = Int32().read_uint32_file(self.data_dir + "/a.bin")
        b = Int32().read_uint32_file(self.data_dir + "/b.bin")
        yield from dut.run_once_sim(x, a, b)


class _ConcurrentTB(_SequentialTB):
    """Violation (c): `main()` SPAWNS the invocation instead of running it inline.

    The ONLY deviation from `_SequentialTB` is `yield from` -> `self.sim.env.process(...)`: the
    invocation now runs *alongside* the rest of `main()` rather than in it.  That is not a missing
    feature of the Vitis lowering, it is a different execution model — there is no `int main()`
    that means this.
    """

    def main(self):
        dut = SimpFunComponent()
        x = Int32().read_uint32_file(self.data_dir + "/x.bin")
        a = Int32().read_uint32_file(self.data_dir + "/a.bin")
        b = Int32().read_uint32_file(self.data_dir + "/b.bin")
        self.sim.env.process(dut.run_once_sim(x, a, b))     # <-- fan-out: a second process


def test_the_sequential_tb_fixture_really_passes():
    """Guard the fixture: the spawn below is only meaningful if the shape it starts from lowers."""
    assert check(_SequentialTB, SEQUENTIAL_VITIS_TB) == (True, None)


def test_gate4_concurrent_tb_is_rejected_and_named_as_concurrent():
    ok, msg = check(_ConcurrentTB, SEQUENTIAL_VITIS_TB)
    assert ok is False
    assert "concurrent" in msg.lower(), "the message must name the CONCURRENCY, not a symptom"
    assert "self.sim.env.process" in msg, "the message must name the offending spawn"
    assert "main()" in msg


def test_gate4_concurrent_tb_message_points_at_the_systemc_path():
    """The message must name the real fix — a different flow, not a missing marker."""
    _, msg = check(_ConcurrentTB, SEQUENTIAL_VITIS_TB)
    assert "SystemC" in msg
    assert "Flow 3" in msg
    assert "docs/guide/flows" in msg, "point at the flow that will support it"
    assert "straight-line" in msg


def test_gate4_concurrent_message_is_better_than_the_old_generic_one():
    """Before Stage 3 this fell through to "Call to non-synthesizable method 'process'".

    That was TRUE and useless: it sends the author off to mark SimPy `@synthesizable`, when the
    real answer is that the testbench belongs on another flow.  The new message must not be that.
    """
    _, msg = check(_ConcurrentTB, SEQUENTIAL_VITIS_TB)
    assert "non-synthesizable method" not in msg
    assert "Mark it @synthesizable or @sim_only" not in msg


def test_gate4_concurrent_rejection_relays_the_extractor_verbatim():
    """The rule is the EXTRACTOR's, not this module's — so `generate` enforces it too.

    If `check` could reject a spawn that `extract_testbench` accepts, the rule would have been
    added to `codegen_check.py`: a shadow that can drift from what codegen does.
    """
    from waveflow.build.hwcodegen import SynthesisError, extract_testbench

    tb = elaborate(_ConcurrentTB)
    with pytest.raises(SynthesisError) as excinfo:
        extract_testbench(tb)

    ok, msg = check(_ConcurrentTB, SEQUENTIAL_VITIS_TB)
    assert ok is False
    assert msg == str(excinfo.value)


# ----------------------------------------------------------------------------------------------
# The detection SHAPE — an attribute call `.process(...)` on an `env`-ish receiver.
#
# Narrow on purpose.  The negatives matter as much as the positives: `waveflow/hw/interface.py`
# and `waveflow/simulation/simobj.py` spawn via `self.process(...)` as a library internal, and a
# rule broad enough to catch that shape would be guessing.
# ----------------------------------------------------------------------------------------------

def _run_spawn_gate(body: str) -> None:
    """Run ONLY the sequential-gate pre-pass over a `main()` built from *body*."""
    from waveflow.build.hwcodegen import HwStmtExtractor

    src = "def main(self):\n" + textwrap.indent(textwrap.dedent(body), "    ")
    func_def = ast.parse(src).body[0]
    HwStmtExtractor(comp=None, method_name="main")._validate_no_concurrency(func_def)


@pytest.mark.parametrize(
    "body",
    [
        "env.process(stim())",
        "self.env.process(stim())",
        "sim.env.process(stim())",
        "self.sim.env.process(stim())",
        "yield env.process(stim())",              # spawned, then joined — still a fork
        "p = self.sim.env.process(stim())",       # bound to a name
        "if x == 1:\n    env.process(stim())",    # nested in a statement
    ],
)
def test_every_env_spawn_spelling_is_caught(body):
    from waveflow.build.hwcodegen import SynthesisError

    with pytest.raises(SynthesisError, match="Concurrent process spawn"):
        _run_spawn_gate(body)


@pytest.mark.parametrize(
    "body",
    [
        # `self.process(...)` — the library-internal spawn wrapper (interface.py, simobj.py).
        # The receiver is not env-ish, so the gate does not fire.  See the report/limitation note:
        # this is a spelling we deliberately do NOT catch.
        "self.process(gen())",
        "yield self.process(self._make_write_call(words))",
        # An `env` receiver, but not the spawn method.
        "yield self.env.timeout(10)",
        "yield self.timeout(self.clk.period)",
        # `.process` on something that is plainly not an environment.
        "self.image.process(frame)",
        "dut.run_once(x)",
    ],
)
def test_the_gate_does_not_fire_on_non_spawns(body):
    _run_spawn_gate(body)     # must not raise


def test_no_real_kernel_or_tb_trips_the_sequential_gate():
    """Stage 3's stated gate: the new rule fires on NOTHING that exists today.

    Driven through the real extractor rather than `check`, because one of the four kernels
    (BlockScaleComponent) is still an un-migrated plain HwComponent that `check` abstains on at
    gate 2 — it would never reach gate 4, so a `check`-level assertion would prove nothing about
    it.  `extract_*` is what `generate` runs, so this covers all four for real.
    """
    from examples.block_scale.block_scale import BlockScaleComponent
    from examples.stream_inband.poly import PolyAccelComponent
    from waveflow.build.hwcodegen import extract_kernel, extract_testbench

    for cls in (SimpFunComponent, PolyAccelComponent, HistAccel, BlockScaleComponent):
        extract_kernel(elaborate(cls))          # must not raise
    for cls in (SimpFunTBHls, PolyTBHls, HistTBHls, BlockScaleTBHls):
        extract_testbench(elaborate(cls))       # must not raise


# ==============================================================================================
# The vocabulary
# ==============================================================================================

def test_implemented_targets_is_a_subset_of_all_targets():
    assert IMPLEMENTED_TARGETS <= ALL_TARGETS


def test_every_declared_potential_target_is_a_known_name():
    """A kind cannot declare a target the vocabulary does not know."""
    for kind in (SimpFunComponent, Square, ScaledSquare, SimpFunTBHls):
        assert potential_targets(kind) <= ALL_TARGETS, kind.__name__


# ==============================================================================================
# The component contract — the STRUCTURAL half (Stage 4)
#
#   A leaf lowers to a standalone Vitis kernel iff
#     (a) it owns no sub-components / internal interfaces   <- these tests
#     (b) its body passes the extractor's rules             <- the gate-4 tests above
# ==============================================================================================

@dataclass
class _HostWithAChild(SimpFunComponent):
    """A HostActivated that also owns a sub-component — structurally not a leaf."""

    cpp_kernel_name: ClassVar[str | None] = "host_with_a_child"
    cpp_namespace: ClassVar[str | None] = "host_with_a_child_impl"

    def __post_init__(self) -> None:
        super().__post_init__()
        self.kid = Square(name=f"{self.name}_kid", sim=self.sim, clk=self.clk)
        self.add_comp(self.kid)


def test_structural_rule_a_leaf_owning_a_subcomponent_is_rejected():
    """Regression for a SILENT failure: this used to emit a kernel and check said (True, None).

    The extractor walks only the entry method, so a leaf carrying a child produced a
    well-formed kernel that never mentioned the child — half the design dropped without a
    word.  The message must name the real fix (CompositeComp), not just refuse.
    """
    ok, msg = check(_HostWithAChild)
    assert ok is False
    assert "single kernel function" in msg
    assert "silently drop" in msg
    assert "CompositeComp" in msg
    assert "_codegen_kid" in msg, "the message must name WHICH child"


def test_structural_rule_lives_in_the_extractor_so_generate_enforces_it_too():
    """The rule must be fail-loud in codegen, not merely reported by check().

    If this ever passes `generate`, the rule has been moved into codegen_check.py — i.e.
    it became a shadow that can drift from what codegen accepts.  See codegen_check's
    module docstring.
    """
    from waveflow.build.hwcodegen import SynthesisError
    from waveflow.build.hwgen import kernel_files_to_str

    with pytest.raises(SynthesisError, match="single kernel function"):
        kernel_files_to_str(_HostWithAChild)


def test_check_relays_the_structural_rule_verbatim():
    """check() must not re-word the extractor — it must BE the extractor's answer."""
    from waveflow.build.hwcodegen import SynthesisError, extract_kernel

    comp = elaborate(_HostWithAChild)
    try:
        extract_kernel(comp)
    except SynthesisError as e:
        raised = str(e)
    else:
        pytest.fail("extract_kernel did not raise")

    assert check(_HostWithAChild) == (False, raised)


def test_the_real_leaves_are_flat_so_the_contract_holds_for_them():
    """(a) holds for every real leaf — the rule is a guard, not a burden."""
    for cls in (SimpFunComponent, HistAccel, Square):
        comp = elaborate(cls)
        assert not comp.sub_comps, cls.__name__
        assert not comp.interfaces, cls.__name__
