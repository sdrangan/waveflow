"""Tests for the toy components the guide quotes (``examples/toy/toy.py``).

These back ``docs/guide/components/freerun.md`` (``Square``) and
``docs/guide/components/composite.md`` (``Double`` -> ``Square`` inside ``ScaledSquare``): the pages
quote this code verbatim, so executing it here is what stops those pages from silently rotting.

Scope is deliberately narrow — elaboration, the pysim golden, and the composite's structure.  These
toys are a **tested pysim model in synthesizable form**, not a generated kernel; see
``test_square_codegen_is_not_yet_a_free_running_task`` for the recorded gap, and ``examples/regmap``
for a real end-to-end (build DAG + Vitis) example.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pytest

from waveflow.hw.clock import Clock
from waveflow.hw.component import Component
from waveflow.hw.interface import StreamIF, StreamIFMaster, StreamIFSlave
from waveflow.simulation.simobj import ProcessGen
from waveflow.simulation.simulation import Simulation

from examples.toy.toy import Double, ScaledSquare, Square, Vec

XS = [
    np.array([1.0, 2.0, 3.0, 4.0], dtype=np.float32),
    np.array([0.5, -1.0, 2.5, 0.0], dtype=np.float32),
]


@dataclass
class VecDriver(Component):
    """Test harness: pushes each Vec in ``vecs`` at the DUT, collects what comes back.

    Being finite is what terminates the sim: a ``FreeRunComp`` loops forever, but once the driver
    returns and every stage is parked on an empty input stream, SimPy's event queue drains and
    ``run_sim()`` returns.  So the received-count assertion below is load-bearing — a deadlock and a
    clean finish look identical to ``run_sim()``.
    """

    vecs: list = field(default_factory=list)
    clk: Clock = field(default_factory=lambda: Clock(freq=100e6))

    def __post_init__(self) -> None:
        super().__post_init__()
        self.m_out = StreamIFMaster(name=f"{self.name}_m_out", sim=self.sim, bitwidth=32)
        self.s_in = StreamIFSlave(name=f"{self.name}_s_in", sim=self.sim, bitwidth=32)
        self.add_endpoint(self.m_out)
        self.add_endpoint(self.s_in)
        self.received: list[np.ndarray] = []

    def run_proc(self) -> ProcessGen[None]:
        for v in self.vecs:
            yield from self.m_out.write(Vec(v))
        for _ in self.vecs:
            y = yield from self.s_in.get(Vec)
            self.received.append(np.asarray(y.val).copy())


def _run(dut_cls, xs=XS):
    """Wire ``driver -> dut -> driver`` over two StreamIFs and run to quiescence."""
    sim = Simulation()
    clk = Clock(freq=100e6)
    dut = dut_cls(name="dut", sim=sim, clk=clk)
    drv = VecDriver(name="drv", sim=sim, vecs=xs, clk=clk)

    in_if = StreamIF(name="in_if", sim=sim, clk=clk, bitwidth=32)
    in_if.bind("master", drv.m_out)
    in_if.bind("slave", dut.x_in)

    out_if = StreamIF(name="out_if", sim=sim, clk=clk, bitwidth=32)
    out_if.bind("master", dut.y_out)
    out_if.bind("slave", drv.s_in)

    sim.run_sim()
    assert len(drv.received) == len(xs), (
        f"expected {len(xs)} outputs, got {len(drv.received)} — the sim went quiet early "
        f"(deadlock looks like a clean finish to run_sim())"
    )
    return dut, drv


# --------------------------------------------------------------------------------------------
# Elaboration — the sim-free codegen entry (also runs the param-purity gate)
# --------------------------------------------------------------------------------------------

@pytest.mark.parametrize("cls", [Square, Double, ScaledSquare])
def test_elaborates(cls):
    from waveflow.build.elaborate import elaborate

    comp = elaborate(cls, {}, name="t")
    assert isinstance(comp, cls)


# --------------------------------------------------------------------------------------------
# The pysim golden
# --------------------------------------------------------------------------------------------

def test_square_pysim_golden():
    """Square: y = x**2, element-wise."""
    _, drv = _run(Square)
    for x, y in zip(XS, drv.received):
        np.testing.assert_allclose(y, x * x, rtol=1e-6)


def test_scaled_square_pysim_golden():
    """ScaledSquare: y = (2x)**2, computed by two concurrent sub-components."""
    _, drv = _run(ScaledSquare)
    for x, y in zip(XS, drv.received):
        np.testing.assert_allclose(y, (2.0 * x) ** 2, rtol=1e-6)


def test_square_body_uses_array_operators_not_numpy():
    """The `x * x` array-operator idiom the doc shows actually runs.

    It returns a *derived* DataArray (element-type-preserving), not a `Vec` — full precision, no
    quantize.  `x * 0.5` / `0.5 * x` are NOT supported (no scalar operands, no __rmul__), which is
    why both toys only ever combine two DataArrays.
    """
    x = Vec(XS[0])
    y = Square(name="s", sim=Simulation()).square(x)
    np.testing.assert_allclose(np.asarray(y.val), XS[0] * XS[0], rtol=1e-6)
    assert y.element_type is Vec.element_type

    with pytest.raises(TypeError, match="both operands to be DataArray"):
        _ = x * 0.5
    with pytest.raises(TypeError, match="unsupported operand"):
        _ = 0.5 * x


# --------------------------------------------------------------------------------------------
# The composite's structure
# --------------------------------------------------------------------------------------------

def test_composite_is_passive_and_schedules_its_children():
    """ScaledSquare has no body; its two children own the concurrent processes."""
    sim = Simulation()
    dut = ScaledSquare(name="dut", sim=sim, clk=Clock(freq=100e6))

    assert dut.run_proc() is None                                  # passive parent
    assert list(dut.sub_comps) == ["dut_double", "dut_square"]     # insertion order
    # Each child registered itself with the Simulation, so run_sim() schedules its run_proc.
    assert dut.double in sim._sim_objs and dut.square in sim._sim_objs
    assert dut.double.run_proc() is not None and dut.square.run_proc() is not None


def test_composite_boundary_ports_alias_their_children():
    """The composite's ports ARE its children's endpoints — no runtime hop."""
    dut = ScaledSquare(name="dut", sim=Simulation(), clk=Clock(freq=100e6))
    assert dut.x_in is dut.double.x_in
    assert dut.y_out is dut.square.y_out


def test_composite_internal_edge_is_bound_between_the_children():
    """The z edge is a real channel: double.z_out (master) -> square.x_in (slave)."""
    dut = ScaledSquare(name="dut", sim=Simulation(), clk=Clock(freq=100e6))
    z_if = dut.interfaces["dut_z_if"]
    assert z_if.endpoints["master"] is dut.double.z_out
    assert z_if.endpoints["slave"] is dut.square.x_in


def test_composite_internal_edge_carries_data():
    """The doubled value really crosses z: spy on what the squarer is handed each firing.

    Its input can only have arrived over the internal edge, so seeing 2*x here is direct evidence
    the channel carried the data (rather than inferring it from the end-to-end result).
    """
    sim = Simulation()
    clk = Clock(freq=100e6)
    dut = ScaledSquare(name="dut", sim=sim, clk=clk)
    drv = VecDriver(name="drv", sim=sim, vecs=XS, clk=clk)

    in_if = StreamIF(name="in_if", sim=sim, clk=clk, bitwidth=32)
    in_if.bind("master", drv.m_out)
    in_if.bind("slave", dut.x_in)
    out_if = StreamIF(name="out_if", sim=sim, clk=clk, bitwidth=32)
    out_if.bind("master", dut.y_out)
    out_if.bind("slave", drv.s_in)

    # Shadow the squarer's compute with a recording pass-through.
    seen: list[np.ndarray] = []
    inner = dut.square.square

    def spy(x):
        seen.append(np.asarray(x.val).copy())
        return inner(x)

    dut.square.square = spy

    sim.run_sim()

    assert len(seen) == len(XS), f"squarer fired {len(seen)}x, expected {len(XS)}"
    for x, z in zip(XS, seen):
        np.testing.assert_allclose(z, 2.0 * x, rtol=1e-6)


def test_streamif_requires_clk():
    """Regression pin for the defect that motivated this: composite.md omitted `clk=`.

    StreamIF models transfer latency as nwords/clk.freq, so QueuedTransferIF.__post_init__ rejects a
    clock-less interface.  The doc's original snippet could never have run.
    """
    with pytest.raises(ValueError, match="clock must be provided"):
        StreamIF(name="z", sim=Simulation(), bitwidth=32)


# --------------------------------------------------------------------------------------------
# Codegen — what the toys do NOT claim
# --------------------------------------------------------------------------------------------

def test_square_codegen_is_not_yet_a_free_running_task():
    """Characterization test: `kernel_files_to_str(Square)` SUCCEEDS but does not yet emit what
    `freerun.md` describes.  Recorded, not papered over (plans/toy_examples.md).

    Two live gaps, both silent (no exception):

    1. The `@synthesizable square` body is NOT extracted — `@synthesizable` marks a *hook boundary*,
       so codegen emits a `// TODO: implement square` stub for a hand-written impl (the same
       arrangement as the checked-in `examples/regmap/simp_fun_compute_impl.cpp`).  The `x * x`
       array-operator body is a pysim golden only; it does not lower to C++.
    2. The top is `ap_ctrl_hs`, NOT the free-running `ap_ctrl_none` `hls::task` the page describes —
       `ap_ctrl_none` is emitted nowhere in `waveflow/build/`.  `codegen_dispatch` names the method
       to extract but explicitly leaves the target/pragma axis for later.
    3. The emitted header is **not valid C++**.  `Square` does not set `cpp_namespace`, so
       `hwgen.resolved_namespace` defaults it to the *kernel name* — emitting `void square(...)` and
       `namespace square {...}` into the same scope, which is ill-formed ("redeclared as different
       kind of entity").  Every real component dodges this by hand-setting `<kernel>_impl`
       (`simp_fun_impl`, `hist_impl`, `poly_impl`, `mem_r_stream_impl`, ...) — a 100% opt-out rate,
       which is itself the evidence that the default is unusable whenever a hook exists.  The toy is
       the first component to take the default, so it is the first to expose it.

    When any gap closes this test fails loudly — which is the point: that is the signal to update
    the doc.  This is what `check(Square, "free_running_kernel")` is meant to report once
    plans/codegen_check_family.md lands.
    """
    from waveflow.build.hwgen import kernel_files_to_str

    files = kernel_files_to_str(Square)
    assert set(files) == {"square.hpp", "square.cpp", "square_square_impl.cpp"}

    # Gap 1: the compute body is a stub, not the extracted `x * x`.
    assert "// TODO: implement square" in files["square_square_impl.cpp"]

    # Gap 2: host-activated control, not a free-running task.
    assert "s_axilite port=return" in files["square.cpp"]
    assert "ap_ctrl_none" not in files["square.cpp"]
    assert "hls::task" not in files["square.cpp"]

    # Gap 3: the namespace collides with the kernel function name -> ill-formed C++.
    assert "void square(" in files["square.hpp"]
    assert "namespace square {" in files["square.hpp"]


def test_scaled_square_declares_no_codegen_descriptors():
    """Characterization test: the composite toy is the pysim *shape*, not a generated top.

    `composite_top_spec` builds the top from the `ordered_subcomps` / `internal_edges` / `boundary`
    descriptors that the real composites (`MemCopy`, `InterleaverCanon`) carry by hand alongside
    their `add_comp`/`add_if` graph.  The toy deliberately declares none of them — backing the claim
    made in docs/guide/components/composite.md.  Adding them is out of scope (plans/toy_examples.md).
    """
    from examples.interleaver.composite_gen import composite_top_spec

    dut = ScaledSquare(name="ss", sim=Simulation(), clk=Clock(freq=100e6))
    for attr in ("ordered_subcomps", "internal_edges", "boundary"):
        assert not hasattr(dut, attr)

    with pytest.raises(AttributeError, match="internal_edges"):
        composite_top_spec(dut, width=32)


# --------------------------------------------------------------------------------------------
# Doc sync — the guide quotes this file verbatim, so prove it still does
# --------------------------------------------------------------------------------------------

DOCS = Path(__file__).resolve().parents[2] / "docs" / "guide" / "components"
TOY_SRC = Path(__file__).resolve().parents[2] / "examples" / "toy" / "toy.py"


@pytest.mark.parametrize("page", ["freerun.md", "composite.md"])
def test_doc_python_blocks_are_verbatim_from_toy(page):
    """Every ```python block on these pages must appear verbatim in examples/toy/toy.py.

    This is what makes the pages un-rottable: edit the toy without editing the doc (or paraphrase
    the doc) and this fails.
    """
    text = (DOCS / page).read_text(encoding="utf-8")
    src = TOY_SRC.read_text(encoding="utf-8")

    blocks = re.findall(r"^```python\n(.*?)^```", text, re.MULTILINE | re.DOTALL)
    assert blocks, f"{page} has no python blocks — did the example move?"

    for block in blocks:
        assert block.strip("\n") in src, (
            f"{page}: this quoted block is not verbatim in {TOY_SRC.name} — "
            f"copy it, do not paraphrase:\n{block}"
        )
