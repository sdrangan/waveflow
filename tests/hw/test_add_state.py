"""Tests for ``HwModule.add_state`` — declared cross-firing state (plans/add_state.md).

Covers the registry, the extractor's capture-rule entry, the call-site lowering, the
instance-resolved hook-argument type, and the ``static`` emission at both sites (the
``ap_ctrl_chain`` kernel top and the generated ``hls::task`` body).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import ClassVar

import pytest

from waveflow.build.hwcodegen import SynthesisError
from waveflow.build.hwgen import (
    state_pragmas,
    hook_signature_str,
    kernel_body_to_cpp,
    state_decls_to_cpp,
    task_files_to_str,
)
from waveflow.hw.clock import Clock
from waveflow.hw.dataschema import DataArray, FloatField, IntField
from waveflow.hw.fixpoint import FixedField
from waveflow.hw.hw_freerun import FreeRunMod
from waveflow.hw.hw_module import discover_state, state_entry_for
from waveflow.hw.hw_state import HwState
from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
from waveflow.hw.synth import synthesizable
from waveflow.simulation.simobj import ProcessGen
from waveflow.simulation.simulation import Simulation

Float32 = FloatField.specialize(bitwidth=32)
Int32 = IntField.specialize(bitwidth=32, signed=True)

#: The stream payload (struct storage — passed by value).
class Vec(DataArray):
    element_type = Float32
    static = True
    max_shape = (4,)


#: The state array (raw storage — lowers to ``float total[4]``).
AccArray = DataArray.specialize(Float32, max_shape=(4,), cpp_storage="raw")


@dataclass
class Accum(FreeRunMod):
    """A stateful leaf: a running per-lane total carried across firings."""

    cpp_kernel_name: ClassVar[str | None] = "accum"

    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.x_in = StreamIFSlave(name=f"{self.name}_x_in", sim=self.sim, bitwidth=32)
        self.y_out = StreamIFMaster(name=f"{self.name}_y_out", sim=self.sim, bitwidth=32)
        self.add_endpoint(self.x_in)
        self.add_endpoint(self.y_out)
        self.total = HwState(AccArray())
        self.add_state(self.total)

    def run_iter(self) -> ProcessGen[None]:
        x = yield from self.x_in.get(Vec)
        y = self.accumulate(x, self.total)
        yield from self.y_out.write(y)

    @synthesizable
    def accumulate(self, x: Vec, total: HwState) -> Vec:
        """Add ``x`` into the running total and return the new total."""
        total.val[:] = total.val + x.val
        return Vec(total.val.copy())


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def test_add_state_records_the_attribute_name():
    """The C++ identifier is the attribute name, discovered by identity."""
    comp = Accum(name="a", sim=Simulation())
    state = discover_state(comp)
    assert list(state) == ["total"]
    assert state["total"] is comp.total


def test_state_entry_for_is_identity_not_equality():
    """Two equal-valued arrays are different storage; only the registered object matches."""
    comp = Accum(name="a", sim=Simulation())
    assert state_entry_for(comp, comp.total) is not None
    assert state_entry_for(comp, HwState(AccArray())) is None


def test_add_state_rejects_an_unbound_object():
    """An object with no attribute has no name to emit — that is an error, not a guess."""
    comp = Accum(name="a", sim=Simulation())
    with pytest.raises(ValueError, match="not bound to an attribute"):
        comp.add_state(HwState(AccArray()))


def test_add_state_rejects_a_bare_schema():
    """The hardware facts live on HwState, so the registry only accepts one."""
    comp = Accum(name="a", sim=Simulation())
    with pytest.raises(TypeError, match="expects an HwState"):
        comp.add_state(AccArray())


def test_a_component_with_no_state_costs_nothing():
    """discover_state is empty and the declaration block is empty for a stateless module."""
    from examples.toy.toy import Square

    comp = Square(name="s", sim=Simulation())
    assert discover_state(comp) == {}
    assert state_decls_to_cpp(comp) == ""


# ---------------------------------------------------------------------------
# The capture rule
# ---------------------------------------------------------------------------


def test_undeclared_self_read_still_raises():
    """add_state does not relax the rule — an undeclared self.X is still rejected."""

    @dataclass
    class Undeclared(Accum):
        cpp_kernel_name: ClassVar[str | None] = "undeclared"

        def __post_init__(self) -> None:
            super().__post_init__()
            self.gain = 2

        def run_iter(self) -> ProcessGen[None]:
            x = yield from self.x_in.get(Vec)
            y = self.scale(x, self.gain)
            yield from self.y_out.write(y)

        @synthesizable
        def scale(self, x: Vec, gain: int) -> Vec:
            return x

    with pytest.raises(SynthesisError, match="Implicit capture of 'self.gain'"):
        task_files_to_str(Undeclared)


def test_capture_error_points_at_add_state():
    """The message tells the author about the affordance, not just the refusal."""

    @dataclass
    class Undeclared2(Accum):
        cpp_kernel_name: ClassVar[str | None] = "undeclared2"

        def __post_init__(self) -> None:
            super().__post_init__()
            self.gain = 2

        def run_iter(self) -> ProcessGen[None]:
            x = yield from self.x_in.get(Vec)
            y = self.scale(x, self.gain)
            yield from self.y_out.write(y)

        @synthesizable
        def scale(self, x: Vec, gain: int) -> Vec:
            return x

    with pytest.raises(SynthesisError, match=r"self\.add_state\(self\.gain\)"):
        task_files_to_str(Undeclared2)


def test_declared_state_read_is_admitted():
    """The whole point: self.total extracts cleanly once declared."""
    files = task_files_to_str(Accum)
    assert "accum_task.h" in files


# ---------------------------------------------------------------------------
# Emission — the hls::task body (Stage 2 site)
# ---------------------------------------------------------------------------


def test_task_body_declares_the_static():
    src = task_files_to_str(Accum)["accum_task.h"]
    assert "static float total[4];" in src


def test_task_body_static_precedes_the_body():
    """The declaration leads the body — an hls::task has no 'before the loop'."""
    src = task_files_to_str(Accum)["accum_task.h"]
    assert src.index("static float total[4];") < src.index("accum_impl::accumulate(")


def test_call_site_passes_the_bare_identifier():
    """self.total lowers to `total` — the same name the declaration used."""
    src = task_files_to_str(Accum)["accum_task.h"]
    assert "accumulate(x, total)" in src


def test_no_access_mode_is_emitted():
    """Read/write permission is a hook's property, not the storage's — nothing claims it here.

    A comment asserting an access mode that nothing checks would read as a guarantee.
    """
    src = task_files_to_str(Accum)["accum_task.h"]
    assert "access=" not in src


# ---------------------------------------------------------------------------
# Decision 5 — the type comes from the instance, not the annotation
# ---------------------------------------------------------------------------


@dataclass
class SweptAccum(Accum):
    """State whose element format is built per instance (the HwParam -> type bridge)."""

    cpp_kernel_name: ClassVar[str | None] = "swept"

    def __post_init__(self) -> None:
        super().__post_init__()
        self.total = HwState(DataArray.specialize(
            FixedField.specialize(W=18, I=2), max_shape=(8,), cpp_storage="raw",
        )())
        self.add_state(self.total)


def test_state_type_resolves_from_the_instance_not_the_annotation():
    """`total: DataArray` in the hook signature; the emitted type is the specialized class."""
    comp = SweptAccum(name="s", sim=Simulation())
    decls = state_decls_to_cpp(comp)
    assert "static ap_fixed<18, 2, AP_TRN, AP_WRAP> total[8];" in decls


def test_hook_signature_uses_the_instance_type():
    """hook_signature must not fall back to the bare DataArray annotation."""
    comp = SweptAccum(name="s", sim=Simulation())
    sig = hook_signature_str(comp.accumulate)
    assert "ap_fixed<18, 2, AP_TRN, AP_WRAP> total[8]" in sig


# ---------------------------------------------------------------------------
# Emission — the ap_ctrl_chain kernel top (Stage 1 site)
# ---------------------------------------------------------------------------


def test_host_activated_top_declares_the_static():
    """The poly-shaped flow: statics lead the kernel function body."""
    from tests.hw.state_poly_fixture import PolyStateAccel

    body = kernel_body_to_cpp(PolyStateAccel(name="p", sim=Simulation()))
    assert "static float coeffs[4];" in body
    assert body.index("static float coeffs[4];") < body.index("evaluate(")


def test_host_activated_call_site_uses_the_identifier():
    from tests.hw.state_poly_fixture import PolyStateAccel

    body = kernel_body_to_cpp(PolyStateAccel(name="p", sim=Simulation()))
    assert "evaluate(cmd_hdr, s_in, m_out, coeffs)" in body


def test_state_coeffs_are_absent_from_the_kernel_signature():
    """State is internal storage — unlike the regmap array, it is NOT a top-level port."""
    from waveflow.build.hwgen import kernel_signature

    from tests.hw.state_poly_fixture import PolyStateAccel

    sig = kernel_signature(PolyStateAccel(name="p", sim=Simulation()))
    assert "coeffs" not in sig


# ---------------------------------------------------------------------------
# The Stage-1 gate: the retrofit must not disturb the path poly already exercises
# ---------------------------------------------------------------------------


def test_gate_hook_signature_is_identical_to_the_regmap_version():
    """THE Stage-1 gate.  Same hook, same C++ signature — only the storage class changed.

    Anchoring on an existing Vitis-verified design is the point: if ``add_state`` had perturbed
    the array lowering, the widths, or the argument order, this is where it would show.
    """
    from examples.stream_inband.poly import PolyAccel

    from tests.hw.state_poly_fixture import PolyStateAccel

    regmap = hook_signature_str(PolyAccel(name="a", sim=Simulation()).evaluate)
    state = hook_signature_str(PolyStateAccel(name="b", sim=Simulation()).evaluate)
    assert regmap == state


def test_gate_body_differs_only_by_the_declaration():
    """The regmap 'already in scope' comment becomes a static; everything else is unchanged."""
    from examples.stream_inband.poly import PolyAccel

    from tests.hw.state_poly_fixture import PolyStateAccel

    def normalize(body: str, ns: str) -> list[str]:
        out = []
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("static float coeffs"):
                continue
            out.append(line.replace(f"{ns}::", ""))
        return out

    regmap = normalize(kernel_body_to_cpp(PolyAccel(name="a", sim=Simulation())), "poly_impl")
    state = normalize(
        kernel_body_to_cpp(PolyStateAccel(name="b", sim=Simulation())), "poly_state_impl",
    )
    assert regmap == state


def test_generated_state_declaration_is_ascii():
    """Generated C++ stays ASCII (the '--' convention the other emitters use)."""
    src = task_files_to_str(Accum)["accum_task.h"]
    src.encode("ascii")  # raises if a stray em-dash sneaks into an emitted comment


# ---------------------------------------------------------------------------
# Stage-2 semantics: the carry actually persists across firings in pysim
# ---------------------------------------------------------------------------


def test_pysim_state_persists_across_firings():
    """Three firings, one running total — the behaviour the static has to reproduce in RTL."""
    import numpy as np

    from waveflow.hw.interface import StreamIF
    from waveflow.simulation.simobj import SimObj

    @dataclass
    class Driver(SimObj):
        clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

        def __post_init__(self) -> None:
            super().__post_init__()
            self.m_out = StreamIFMaster(name=f"{self.name}_m", sim=self.sim, bitwidth=32)
            self.s_in = StreamIFSlave(name=f"{self.name}_s", sim=self.sim, bitwidth=32)
            self.seen: list[np.ndarray] = []

        def run_proc(self) -> ProcessGen[None]:
            for _ in range(3):
                yield from self.m_out.write(Vec(np.ones(4, dtype=np.float32)))
                got = yield from self.s_in.get(Vec)
                self.seen.append(np.asarray(got.val).copy())

    sim = Simulation()
    clk = Clock(freq=100e6)
    dut = Accum(name="dut", sim=sim, clk=clk)
    drv = Driver(name="drv", sim=sim, clk=clk)
    a = StreamIF(name="a", sim=sim, clk=clk, bitwidth=32)
    a.bind("master", drv.m_out)
    a.bind("slave", dut.x_in)
    b = StreamIF(name="b", sim=sim, clk=clk, bitwidth=32)
    b.bind("master", dut.y_out)
    b.bind("slave", drv.s_in)
    sim.run_sim()

    # 1, 2, 3 — the running total, not three copies of the input.
    assert [float(v[0]) for v in drv.seen] == [1.0, 2.0, 3.0]


# ---------------------------------------------------------------------------
# Pragma metadata — declared on the HwState, not on its schema
# ---------------------------------------------------------------------------


@dataclass
class PartAccum(Accum):
    cpp_kernel_name: ClassVar[str | None] = "part_accum"

    def __post_init__(self) -> None:
        super().__post_init__()
        self.total = HwState(AccArray(),
                             partition={"type": "complete"},
                             bind_storage={"type": "RAM_2P", "impl": "BRAM"})
        self.add_state(self.total)


def test_partition_pragma_follows_the_declaration():
    decls = state_decls_to_cpp(PartAccum(name="p", sim=Simulation()))
    lines = [ln.strip() for ln in decls.splitlines()]
    i = lines.index("static float total[4];")
    assert lines[i + 1] == "#pragma HLS ARRAY_PARTITION variable=total complete dim=1"
    assert lines[i + 2] == "#pragma HLS BIND_STORAGE variable=total type=RAM_2P impl=BRAM"


def test_two_states_of_one_schema_can_partition_differently():
    """THE reason the specs live on HwState: partitioning is a property of the storage, not of
    the data type.  On the schema this would be impossible — a schema is shared and cached."""
    a = HwState(AccArray(), partition={"type": "complete"})
    b = HwState(AccArray(), partition={"type": "cyclic", "factor": 2})
    a.name, b.name = "a", "b"
    assert a.schema is b.schema
    assert state_pragmas(a) == ["#pragma HLS ARRAY_PARTITION variable=a complete dim=1"]
    assert state_pragmas(b) == ["#pragma HLS ARRAY_PARTITION variable=b cyclic factor=2 dim=1"]


def test_no_pragma_declared_means_no_pragma_emitted():
    """Plain storage stays pragma-free rather than getting a 'default' that overrides Vitis."""
    s = HwState(AccArray())
    s.name = "total"
    assert state_pragmas(s) == []


def test_complete_partition_rejects_a_factor():
    with pytest.raises(ValueError, match="takes no factor"):
        HwState(AccArray(), partition={"type": "complete", "factor": 2})


def test_cyclic_partition_requires_a_factor():
    with pytest.raises(ValueError, match="requires an integer factor"):
        HwState(AccArray(), partition={"type": "cyclic"})


def test_unknown_partition_type_is_rejected():
    with pytest.raises(ValueError, match="is invalid; must be one of"):
        HwState(AccArray(), partition={"type": "diagonal"})


def test_unknown_partition_key_is_rejected():
    with pytest.raises(ValueError, match="unknown key"):
        HwState(AccArray(), partition={"type": "complete", "factr": 2})


def test_bind_storage_requires_a_type():
    with pytest.raises(ValueError, match="requires a 'type'"):
        HwState(AccArray(), bind_storage={"impl": "BRAM"})


def test_pragmas_reach_the_generated_task_body():
    src = task_files_to_str(PartAccum)["part_accum_task.h"]
    assert "#pragma HLS ARRAY_PARTITION variable=total complete dim=1" in src


def test_hwstate_val_delegates_to_the_wrapped_instance():
    """The wrapper is invisible to hook arithmetic — that is what keeps hook bodies unchanged."""
    import numpy as np

    s = HwState(AccArray())
    s.val[:] = np.arange(4)
    np.testing.assert_array_equal(np.asarray(s.value.val), np.arange(4))
