"""Tests for `render_ports_h` — the testbench port binding derived from the top's own TopSpec.

The point of this header is a *negative* property: a TB cannot name a port the kernel does not have,
because both come from one spec. These tests pin the derivation rules that make that true, and the
one that makes it useful (the pin-low set must be the complement of what the BFM drives — pinning a
port the TB is supposed to drive would deadlock the run).
"""
from __future__ import annotations

from waveflow.build.composite_gen import (
    TopSpec,
    _axis_port,
    _maxi_port,
    render_ports_h,
)


def _spec(top="k", ports=None) -> TopSpec:
    return TopSpec(top_name=top, ports=tuple(ports or ()), tasks=(), cmd_headers=())


def test_axis_keeps_its_name_maxi_is_named_after_its_bundle():
    """The asymmetry a hand-written TB gets wrong once and then carries forever."""
    spec = _spec(ports=[
        _axis_port("s_cmd", 64, kind="axis_in"),
        _maxi_port("m_in", 64, const=True, bundle="gmem0"),
        _maxi_port("m_out", 64, const=False, bundle="gmem1"),
    ])
    h = render_ports_h(spec)
    assert 'const s_cmd    = "s_cmd";' in h
    # named after the BUNDLE, not the port: m_in lives on gmem0.
    assert 'const m_in     = "m_axi_gmem0";' in h
    assert 'const m_out    = "m_axi_gmem1";' in h
    assert 'const DESIGN_DLL = "xsim.dir/k/xsimk.dll";' in h


def test_design_library_is_named_per_platform():
    """One emitted header has to serve a Windows and a Linux build of the same testbench.

    xelab -dll writes xsimk.dll on Windows and xsimk.so on Linux, so the name cannot be baked in
    at generation time — the host that GENERATES the header is not necessarily the host that
    compiles the TB against it.
    """
    h = render_ports_h(_spec(ports=[_axis_port("s_cmd", 64, kind="axis_in")]))
    assert "#ifdef _WIN32" in h
    assert 'const DESIGN_DLL = "xsim.dir/k/xsimk.dll";' in h
    assert 'const DESIGN_DLL = "xsim.dir/k/xsimk.so";' in h
    assert "#endif" in h
    # Exactly one definition is live per compile: the two sit in opposite arms of one conditional.
    assert h.count("DESIGN_DLL") == 2


def test_binding_tracks_the_spec():
    """The drift claim: rename the bundle in Python and the TB's binding follows, with no edit.

    This is the whole reason the header exists. Before, a renamed bundle left the TB compiling
    happily against a port that no longer existed — and the failure surfaced as a hang.
    """
    before = render_ports_h(_spec(ports=[_maxi_port("m_in", 64, const=True, bundle="gmem0")]))
    after = render_ports_h(_spec(ports=[_maxi_port("m_in", 64, const=True, bundle="gmem7")]))
    assert 'const m_in     = "m_axi_gmem0";' in before
    assert 'const m_in     = "m_axi_gmem7";' in after
    assert "m_axi_gmem0" not in after and "m_axi_gmem7" not in before


def test_pin_low_set_is_the_complement_of_what_the_bfm_drives():
    """ZERO_PORTS must never contain a port the TB's own model drives — that would deadlock it.

    For a READ bundle the BFM owns ARREADY/R*; for a WRITE bundle it owns AWREADY/WREADY/B*. Each
    kind pins only the other side.
    """
    read_h = render_ports_h(_spec(ports=[_maxi_port("m_in", 64, const=True, bundle="gmem0")]))
    for driven in ("m_axi_gmem0_ARREADY", "m_axi_gmem0_RVALID", "m_axi_gmem0_RDATA",
                   "m_axi_gmem0_RLAST"):
        assert f'"{driven}"' not in read_h, f"{driven} is driven by AxiMmReadSlave; pinning it hangs"
    for pinned in ("m_axi_gmem0_AWREADY", "m_axi_gmem0_WREADY", "m_axi_gmem0_BVALID"):
        assert f'"{pinned}"' in read_h

    write_h = render_ports_h(_spec(ports=[_maxi_port("m_out", 64, const=False, bundle="gmem1")]))
    for driven in ("m_axi_gmem1_AWREADY", "m_axi_gmem1_WREADY", "m_axi_gmem1_BVALID"):
        assert f'"{driven}"' not in write_h, f"{driven} is driven by AxiMmWriteSlave; pinning it hangs"
    for pinned in ("m_axi_gmem1_ARREADY", "m_axi_gmem1_RVALID", "m_axi_gmem1_RDATA"):
        assert f'"{pinned}"' in write_h


def test_control_slave_pinned_only_when_an_maxi_exists():
    """The s_axi_control slave exists because of `offset=slave` on an m_axi port. Pinning it
    quiescent is what makes every offset register read 0 — i.e. element coords == byte addr / BPW."""
    with_maxi = render_ports_h(_spec(ports=[_maxi_port("m_in", 64, const=True, bundle="gmem0")]))
    assert '"s_axi_control_AWVALID"' in with_maxi

    stream_only = render_ports_h(_spec(ports=[_axis_port("s_cmd", 64, kind="axis_in")]))
    assert "s_axi_control" not in stream_only, "a stream-only kernel has no control slave to pin"


def test_generated_header_is_ascii():
    """Generated C++ must be ASCII: written on a cp1252 host and fed to mingw g++."""
    spec = _spec(ports=[
        _axis_port("s_cmd", 64, kind="axis_in"),
        _maxi_port("m_in", 64, const=True, bundle="gmem0"),
    ])
    assert render_ports_h(spec).isascii()


def test_mem_copy_binding_matches_its_real_boundary():
    """End-to-end against the real composite: the binding is derived from the same graph walk that
    emits the top's pragmas, so the two cannot disagree."""
    from waveflow.build.composite_gen import composite_top_spec, render_top
    from waveflow.build.elaborate import elaborate
    from examples.mem_copy.mem_copy import MemCopy

    comp = elaborate(MemCopy, {"mem_dwidth": 64}, name="mem_copy")
    spec = composite_top_spec(comp, width=64)
    h, top = render_ports_h(spec), render_top(spec)

    # every m_axi bundle the top declares a pragma for is bound in the header, by bundle name
    assert "bundle=gmem0" in top and '"m_axi_gmem0"' in h
    assert "bundle=gmem1" in top and '"m_axi_gmem1"' in h
    # every axis port the top declares is bound by its own name
    for axis in ("s_cmd", "s_done"):
        assert f"#pragma HLS INTERFACE axis port={axis}" in top and f'"{axis}"' in h


# ---------------------------------------------------------------------------
# The direction is the TYPE, not a tag beside it -- InterfaceEndpoint's boundary-kind contract.
# (plans/endpoint_types_not_tags.md argued it; completed and deleted in cd6a1ed.)
# ---------------------------------------------------------------------------

def test_kind_is_derived_from_the_endpoint_type():
    """Lowering is a function of the type. Nothing infers; nothing is told separately."""
    from waveflow.build.composite_gen import kind_of_endpoint
    from waveflow.hw.interface import StreamIFMaster, StreamIFSlave
    from waveflow.hw.memif import MMIFReadMaster, MMIFWriteMaster
    from waveflow.simulation.simulation import Simulation

    sim = Simulation()
    assert kind_of_endpoint(StreamIFSlave(name="a", sim=sim, bitwidth=64)) == "axis_in"
    assert kind_of_endpoint(StreamIFMaster(name="b", sim=sim, bitwidth=64)) == "axis_out"
    assert kind_of_endpoint(MMIFReadMaster(name="r", sim=sim, bitwidth=64)) == "maxi_read"
    assert kind_of_endpoint(MMIFWriteMaster(name="w", sim=sim, bitwidth=64)) == "maxi_write"


def test_an_undeclared_maxi_master_is_refused_not_guessed():
    """A bare MMIFMaster under-specifies: is its pointer const or plain?

    Refusing is the point. It is legal hardware (read+write m_axi lowers to a plain pointer with all
    channels), but guessing here is exactly the side-channel this design deletes -- and guessing
    wrong emits a `const` pointer for a port that gets written. It caught a real case on the day it
    landed: the interleaver's IlMemR/IlMemW had never declared their direction either.
    """
    import pytest
    from waveflow.build.composite_gen import kind_of_endpoint
    from waveflow.hw.memif import MMIFMaster
    from waveflow.simulation.simulation import Simulation

    bare = MMIFMaster(name="m", sim=Simulation(), bitwidth=64)
    with pytest.raises(ValueError, match="does not declare a direction"):
        kind_of_endpoint(bare)


def test_a_read_master_refuses_a_write_in_the_model_too():
    """The same declaration enforces at both levels: `const` in the emitted C++, AttributeError in
    the Python model. The restriction is DERIVED from the @port_read/@port_write tags the methods
    already carry, so adding a method to MMIFMaster cannot leave this silently permissive."""
    import pytest
    from waveflow.hw.memif import MMIFMaster, MMIFReadMaster
    from waveflow.simulation.simulation import Simulation

    r = MMIFReadMaster(name="r", sim=Simulation(), bitwidth=64)
    assert isinstance(r, MMIFMaster), "downstream isinstance(ep, MMIFMaster) must keep working"
    assert callable(r.read)
    with pytest.raises(AttributeError, match="is a write operation"):
        r.write


def test_a_stated_kind_must_agree_with_the_type():
    """Legacy 4-tuple boundaries still parse, but a disagreement is an error rather than a silent
    win for one side -- one of the two is wrong, and trusting either is how a const pointer lands on
    a written port."""
    import pytest
    from waveflow.build.composite_gen import _unpack_boundary
    from waveflow.hw.memif import MMIFReadMaster
    from waveflow.simulation.simulation import Simulation

    ep = MMIFReadMaster(name="r", sim=Simulation(), bitwidth=64)
    assert _unpack_boundary(("m_in", ep, "maxi_read", "gmem0")) == ("m_in", ep)
    with pytest.raises(ValueError, match="declares kind 'maxi_write' but its endpoint"):
        _unpack_boundary(("m_in", ep, "maxi_write", "gmem0"))


def test_a_leaf_walks_exactly_like_a_composite():
    """A standalone kernel IS the 1-task degenerate case -- it just could not say so before.

    `top_spec_for` used to be a table keyed on the class that restated the component's own ports,
    their directions and its task signature. Now a leaf declares (FreeRunMod.boundary, ordered by
    kernel_task().signature), its direction is its endpoint's type, and its one task is itself -- so
    composite_top_spec walks it. This pins that the walk still equals what the table produced;
    if it ever diverges, the generated kernels move.
    """
    from waveflow.build.composite_gen import composite_top_spec
    from waveflow.hw.mem_stream import MemRStream, MemWStream
    from waveflow.simulation.simulation import Simulation

    sim = Simulation()
    for cls, top in ((MemRStream, "mem_r_stream"), (MemWStream, "mem_w_stream")):
        leaf = cls(name=top, sim=sim, mem_dwidth=64)
        leaf.cmd_headers = (leaf._cmd_cls.resolved_include_filename(),)
        spec = composite_top_spec(leaf, width=64)

        assert spec.top_name == top
        assert len(spec.tasks) == 1, "a leaf has exactly one task -- itself"
        # The top's C++ parameter list and the task's call args are literally the same list, so they
        # cannot disagree. That is what deriving the order from the signature buys.
        assert spec.tasks[0].args == leaf.kernel_task().signature
        assert tuple(p.name for p in spec.ports) == leaf.kernel_task().signature
        assert not spec.internal_streams, "a leaf wires nothing: every port is a boundary port"

    # And the directions came from the endpoint types, not from a table.
    r = MemRStream(name="mem_r_stream", sim=sim, mem_dwidth=64)
    r.cmd_headers = ()
    kinds = {p.name: p.kind for p in composite_top_spec(r, width=64).ports}
    assert kinds == {"s_cmd": "axis_in", "m_mem": "maxi_read", "m_out": "axis_out"}


def test_bundles_are_assigned_by_policy_not_declared():
    """gmem0, gmem1, ... in boundary declaration order -- the last hand-written field, gone.

    A bundle is an allocation by whoever assembles the top, not a fact about the port: the SAME
    MemWStream.m_mem endpoint is gmem0 standalone and gmem1 inside MemCopy. That is exactly why it
    could not move onto the type with `kind` did, and why a policy beats a declaration -- a policy
    cannot disagree with itself.
    """
    from waveflow.build.composite_gen import bundle_map, composite_top_spec
    from waveflow.hw.mem_stream import MemWStream
    from waveflow.simulation.simulation import Simulation

    # Standalone: the one m_axi port is the first, so gmem0 -- even though it is a WRITE port, which
    # the old code special-cased to gmem1 and then had to override back.
    w = MemWStream(name="mem_w_stream", sim=Simulation(), mem_dwidth=64)
    w.cmd_headers = ()
    assert bundle_map(w.boundary) == {"m_mem": "gmem0"}
    assert [p.bundle for p in composite_top_spec(w, width=64).ports if p.bundle] == ["gmem0"]

    # The same endpoint inside a composite: second m_axi port declared, so gmem1.
    from examples.mem_copy.mem_copy import MemCopy
    mc = MemCopy(name="mem_copy", sim=Simulation(), mem_dwidth=64)
    assert bundle_map(mc.boundary) == {"m_in": "gmem0", "m_out": "gmem1"}
    assert mc.wstream.m_mem is [e[1] for e in mc.boundary if e[0] == "m_out"][0],         "same endpoint object as the standalone case -- only the assembler differs"

    # AXIS ports have no bundle; they are named for themselves.
    assert "s_cmd" not in bundle_map(mc.boundary)


def test_a_stated_bundle_that_contradicts_the_policy_fails_loudly():
    """A legacy 4-tuple may still state a bundle, but if it disagrees with the policy one of them is
    wrong, and picking either silently would put a port on the wrong AXI bundle -- an RTL-level
    misroute that csim cannot see."""
    import pytest
    from waveflow.build.composite_gen import bundle_map
    from waveflow.hw.memif import MMIFReadMaster, MMIFWriteMaster
    from waveflow.simulation.simulation import Simulation

    sim = Simulation()
    r = MMIFReadMaster(name="r", sim=sim, bitwidth=64)
    w = MMIFWriteMaster(name="w", sim=sim, bitwidth=64)
    # Agrees: declaration order gives exactly these.
    assert bundle_map([("m_in", r, "maxi_read", "gmem0"),
                       ("m_out", w, "maxi_write", "gmem1")]) == {"m_in": "gmem0", "m_out": "gmem1"}
    # Contradicts.
    with pytest.raises(ValueError, match="declares bundle .* but the assembler's policy"):
        bundle_map([("m_in", r, "maxi_read", "gmem1")])


# ---------------------------------------------------------------------------
# boundary_kind — the endpoint declares its kind, and inheritance resolves it
# (plans/interface_docs_and_naming.md Part 4)
# ---------------------------------------------------------------------------

def _one_of_every_endpoint():
    """Every endpoint class that has an opinion about being a boundary port, and what it must say.

    ``None`` means DECLARED-but-under-specified (the ``MMIFMaster`` refusal); ``...`` means the
    class declares nothing at all, so it is not a boundary port.  Both are refusals and they are
    different diagnoses, which is why they are distinguished here rather than lumped as "raises".
    """
    from waveflow.hw.bram import BramIFMaster, BramIFSlave
    from waveflow.hw.dataschema import IntField
    from waveflow.hw.interface import (SobIFMaster, SobIFSlave, StreamIFMaster,
                                       StreamIFSlave)
    from waveflow.hw.memif import (MMIFMaster, MMIFReadMaster, MMIFSlave,
                                   MMIFWriteMaster, _DirectionalMMIFMaster)
    from waveflow.hw.regmap import (RegAccess, RegField, RegMap, RegMapMMIFSlave,
                                    VitisRegMap, VitisRegMapMMIFSlave)
    from waveflow.simulation.simulation import Simulation

    sim = Simulation()
    i32 = IntField.specialize(bitwidth=32, signed=True)
    rm = RegMap({"x": RegField(i32, RegAccess.RW)})
    vrm = VitisRegMap({"x": RegField(i32, RegAccess.RW)})

    return [
        # -- boundary ports, each declaring its own kind
        (StreamIFSlave(name="a", sim=sim, bitwidth=64), "axis_in"),
        (StreamIFMaster(name="b", sim=sim, bitwidth=64), "axis_out"),
        (MMIFSlave(name="c", sim=sim, bitwidth=64), "mm_slave"),
        (MMIFReadMaster(name="d", sim=sim, bitwidth=64), "maxi_read"),
        (MMIFWriteMaster(name="e", sim=sim, bitwidth=64), "maxi_write"),
        (BramIFMaster(name="f", sim=sim), "bram"),
        # -- THE ORDERING HAZARD.  In the isinstance chain this replaced, RegMapMMIFSlave had to be
        #    tested before MMIFSlave and MMIFReadMaster before MMIFMaster.  Swap two lines and an
        #    axilite_slave lowered as mm_slave with no error.  Both subclass pairs are here.
        (RegMapMMIFSlave(name="g", sim=sim, bitwidth=32, regmap=rm), "axilite_slave"),
        (VitisRegMapMMIFSlave(name="h", sim=sim, bitwidth=32, regmap=vrm,
                              on_start=lambda: None), "axilite_slave"),
        # -- declared, and under-specified: the direction is the type, and this type does not say
        (MMIFMaster(name="i", sim=sim, bitwidth=64), None),
        (_DirectionalMMIFMaster(name="j", sim=sim, bitwidth=64), None),
        # -- not boundary ports at all, and they declare nothing
        (BramIFSlave(name="k", sim=sim), ...),
        (SobIFMaster(name="l", sim=sim), ...),
        (SobIFSlave(name="m", sim=sim), ...),
    ]


def test_every_endpoint_class_resolves_to_the_kind_it_declares():
    """One case per endpoint class, subclasses included — the table the chain could not have.

    ``kind_of_endpoint`` is now a lookup of ``ep.boundary_kind``, so the answer comes from the class
    and inheritance resolves subclass-before-base by construction.  The two subclass pairs that the
    old ``isinstance`` chain depended on the ORDER of its lines to get right are the point of this
    test: ``RegMapMMIFSlave``/``VitisRegMapMMIFSlave`` under ``MMIFSlave``, and
    ``MMIFReadMaster``/``MMIFWriteMaster``/``_DirectionalMMIFMaster`` under ``MMIFMaster``.
    """
    import pytest
    from waveflow.build.composite_gen import kind_of_endpoint

    for ep, expected in _one_of_every_endpoint():
        name = type(ep).__name__
        if expected is ...:
            with pytest.raises(ValueError, match="no boundary kind for endpoint type"):
                kind_of_endpoint(ep)
        elif expected is None:
            with pytest.raises(ValueError, match="does not declare a direction"):
                kind_of_endpoint(ep)
        else:
            assert kind_of_endpoint(ep) == expected, f"{name} resolved to the wrong kind"


def test_a_subclass_that_declares_nothing_inherits_its_base_kind():
    """The property the isinstance chain did NOT have, stated on its own.

    A new endpoint subclass is correct by default — it lowers like the thing it specializes — and a
    subclass that means something else says so, once, on itself.  Neither outcome depends on where a
    line sits in a chain in another package.
    """
    from waveflow.build.composite_gen import kind_of_endpoint
    from waveflow.hw.interface import StreamIFSlave
    from waveflow.simulation.simulation import Simulation

    class _TaggedStreamSlave(StreamIFSlave):
        """A specialization that changes behaviour, not port kind."""

    class _NotReallyAStream(StreamIFSlave):
        boundary_kind = "axis_out"

    sim = Simulation()
    assert kind_of_endpoint(_TaggedStreamSlave(name="a", sim=sim, bitwidth=64)) == "axis_in"
    assert kind_of_endpoint(_NotReallyAStream(name="b", sim=sim, bitwidth=64)) == "axis_out"


def test_the_two_refusals_are_different_diagnoses():
    """``None`` and "not declared" both refuse, and a reader must be able to tell them apart.

    ``None`` says *this type under-specifies its port* — the fix is to construct a directional
    subclass.  Absence says *this is not a boundary port at all* — a ``BramIFSlave`` is the far end
    of a wrapper wire, and there is nothing to fix.  A single message would send the second reader
    looking for a direction that does not exist.
    """
    import pytest
    from waveflow.build.composite_gen import kind_of_endpoint
    from waveflow.hw.bram import BramIFSlave
    from waveflow.hw.memif import MMIFMaster
    from waveflow.simulation.simulation import Simulation

    sim = Simulation()
    with pytest.raises(ValueError) as under:
        kind_of_endpoint(MMIFMaster(name="m", sim=sim, bitwidth=64))
    with pytest.raises(ValueError) as absent:
        kind_of_endpoint(BramIFSlave(name="b", sim=sim))

    assert "MMIFReadMaster or MMIFWriteMaster" in str(under.value)
    assert "is not a kernel boundary port" in str(absent.value)
    assert "does not declare a direction" not in str(absent.value)
