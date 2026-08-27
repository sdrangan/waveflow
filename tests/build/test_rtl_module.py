"""The ``rtl_module`` target: the hook, the port-name contract, and the latency single source.

**The witness is the oracle here, not this file.**  ``plans/witness/t2p_bram/`` holds four
hand-written files that were csynthed and run in xsim before any of this infrastructure existed, and
the ramp they verified is what makes their port names and their read latency *facts* rather than
recollections.  So the two gates that matter below compare what Waveflow derives against what
actually ran:

* :func:`test_derived_port_names_match_the_witness` — every net the witness's ``rx_top.v`` connects
  on the kernel's two ``bram`` interfaces, against :func:`bram_port_signals`.  Toolchain-free: it
  reads a committed file.
* :func:`test_shipped_memory_is_the_witness_plus_the_published_latency` — the shipped ``.v`` against
  the witness's, byte for byte modulo the one block that was added.

Everything else here is refusals, and each asserts the message names *what* is wrong — an endpoint
kind with no port mapping must be refused by name, not by ``KeyError`` from inside a walk.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from waveflow.build.codegen_check import check
from waveflow.build.composite_gen import _bram_port, bram_port_signals
from waveflow.build.elaborate import ElabContext, elaborate
from waveflow.build.hwcodegen import LoweringError
from waveflow.build.rtl_gen import (
    RTL_SRC_DIR,
    RtlModule,
    resolve_rtl_module,
    rtl_port_mapping,
    rtl_read_latency,
    verilog_module_ports,
)
from waveflow.hw.bram import BramIFSlave, T2pBram, ramb18_count, word_element
from waveflow.hw.codegen_targets import (
    ALL_TARGETS,
    CUT_INDEPENDENT_TARGETS,
    IMPLEMENTED_TARGETS,
    REALIZATION_HOOKS,
    RTL_MODULE,
)
from waveflow.hw.hw_module import HwModule, declares_hook
from waveflow.hw.interface import StreamIFSlave

REPO = Path(__file__).resolve().parents[2]
WITNESS = REPO / "plans" / "witness" / "t2p_bram"


# ---------------------------------------------------------------------------
# The witness gates
# ---------------------------------------------------------------------------

def test_derived_port_names_match_the_witness():
    """The port-name chain, checked against the instantiation that ran.

    Waveflow endpoint -> C++ parameter name -> Vitis port names.  The witness's kernel had two
    ``bram`` array parameters, ``buf_w`` and ``buf_r``; ``rx_top.v`` connects every port Vitis
    emitted for them.  Derivation and reality must agree on all 28, with nothing extra and nothing
    missing — an extra name is a net that does not exist, a missing one is a wire nobody drives.
    """
    top = (WITNESS / "rx_top.v").read_text(encoding="utf-8")
    inst = top[top.index("rx kernel ("):]
    inst = inst[:inst.index(");")]
    connected = {m.group(1) for m in re.finditer(r"\.(\w+)\s*\(", inst)}
    bram_nets = {n for n in connected if n.startswith(("buf_w_", "buf_r_"))}

    derived = set(bram_port_signals("buf_w").values()) | set(bram_port_signals("buf_r").values())
    assert derived == bram_nets, (
        "the derived bram port names must be exactly what the witness's kernel instantiation "
        f"connects; only in the witness: {sorted(bram_nets - derived)}, only derived: "
        f"{sorted(derived - bram_nets)}")
    assert len(bram_nets) == 28, "two interfaces x seven signals x an A/B pair"


def test_one_interface_becomes_fourteen_nets_an_a_b_pair_of_seven():
    """One C++ array parameter, four physical ports' worth of pins.

    Vitis emits a true-dual-port pair per ``bram`` interface whether or not the kernel uses both
    halves.  The witness wires the A half of each and ties the B halves off, which is a wiring
    question the derivation must not pre-empt: it names all fourteen and lets the wrapper choose.
    """
    sigs = bram_port_signals("buf_w")
    assert len(sigs) == 14
    assert sigs["Addr_A"] == "buf_w_Addr_A"
    assert sigs["WEN_B"] == "buf_w_WEN_B"
    assert set(sigs) == {f"{s}_{h}" for h in ("A", "B")
                         for s in ("Addr", "EN", "Din", "Dout", "WEN", "Clk", "Rst")}


def test_shipped_memory_is_the_witness_plus_the_published_latency():
    """ACCEPTANCE: the Verilog placed for a 1024x16 buffer **is** the witness's memory.

    The witness ran.  So the artifact is a copy, and the only permitted difference is the block that
    publishes ``READ_LATENCY`` — the line that makes the C++ pragma derivable from the file instead
    of authored beside it.  Removing that block must recover the witness byte for byte, line endings
    included.
    """
    import difflib

    shipped = (RTL_SRC_DIR / "bram_t2p.v").read_bytes().decode().splitlines(keepends=True)
    witness = (WITNESS / "bram_t2p.v").read_bytes().decode().splitlines(keepends=True)

    changes = [op for op in difflib.SequenceMatcher(None, witness, shipped).get_opcodes()
               if op[0] != "equal"]
    assert len(changes) == 1 and changes[0][0] == "insert", (
        f"the shipped memory must differ from the witness by ONE insertion, got {changes}")
    added = shipped[changes[0][3]:changes[0][4]]
    assert any("localparam READ_LATENCY = 1;" in ln for ln in added)
    assert all(ln.strip() == "" or ln.lstrip().startswith("//") or "localparam" in ln
               for ln in added), (
        "the insertion may publish the latency and explain itself, and do nothing else: "
        f"{added}")
    assert all(ln.endswith("\r\n") for ln in added), "line endings match the file it was added to"


def test_the_read_during_write_assertion_survived_becoming_an_artifact():
    """The design invariant is checked where nothing else would check it.

    ``rd`` trails ``wr`` is the correctness argument for a circular buffer, and the tool does not own
    it here — the designer does.  If the reader ever touches the address the writer is writing, the
    data is whatever the BRAM's read-during-write mode happens to be and no tool says a word.  A
    hand-written memory can assert it; an emulated one cannot.
    """
    text = (RTL_SRC_DIR / "bram_t2p.v").read_text(encoding="utf-8")
    assert "$error" in text
    assert "read-during-write collision" in text


# ---------------------------------------------------------------------------
# Latency: one number, two halves
# ---------------------------------------------------------------------------

def test_pragma_latency_is_read_from_the_verilog_not_authored_beside_it():
    mem = elaborate(T2pBram)
    assert mem.read_latency == 1
    port = _bram_port("buf_w", 16, 1024, latency=mem.read_latency)
    assert "latency=1" in port.pragmas[0]


def test_changing_the_verilog_changes_the_pragma_and_nothing_else_can(tmp_path):
    """The single-source property, demonstrated rather than asserted.

    A memory whose file publishes ``READ_LATENCY = 2`` yields ``latency=2`` in the pragma, with no
    Python change anywhere.  And the converse is what makes it a *single* source: there is no
    latency field on the module or on the endpoint to set, so the two halves cannot be authored
    independently — the only way to change the pragma is to change the Verilog it describes.
    """
    src = (RTL_SRC_DIR / "bram_t2p.v").read_text(encoding="utf-8")
    lat2 = tmp_path / "bram_t2p.v"
    lat2.write_text(src.replace("localparam READ_LATENCY = 1;", "localparam READ_LATENCY = 2;"),
                    encoding="utf-8")

    rtl = RtlModule(module="bram_t2p", files=(str(lat2),))
    assert rtl_read_latency(rtl) == 2
    assert "latency=2" in _bram_port("buf_w", 16, 1024, latency=rtl_read_latency(rtl)).pragmas[0]

    mem = elaborate(T2pBram)
    assert not hasattr(type(mem).read_latency, "__set__") or isinstance(
        type(mem).read_latency, property), "read_latency is derived, not stored"
    with pytest.raises(AttributeError):
        mem.read_latency = 2          # a property with no setter: there is nothing to disagree


def test_a_memory_whose_verilog_publishes_no_latency_is_refused(tmp_path):
    src = (RTL_SRC_DIR / "bram_t2p.v").read_text(encoding="utf-8")
    silent = tmp_path / "bram_t2p.v"
    silent.write_text(re.sub(r"\s*localparam READ_LATENCY = 1;", "", src), encoding="utf-8")

    class _SilentMem(T2pBram):
        def rtl_module(self):
            return super().rtl_module().__class__(
                module="bram_t2p", files=(str(silent),),
                ports=super().rtl_module().ports, clock="clk")

    ok, msg = check(_SilentMem, RTL_MODULE)
    assert ok is False
    assert "READ_LATENCY" in msg and "ONE number" in msg


# ---------------------------------------------------------------------------
# The sized-array trap
# ---------------------------------------------------------------------------

def test_the_bram_pragma_is_emitted_against_a_sized_array_never_a_pointer():
    """``mode=bram`` on an *unsized* pointer produced an ``ap_vld`` scalar port — with no warning.

    The pragma is silently ignored, the kernel elaborates, and the memory is simply not there.  So
    the emitted C++ declaration carries the depth, and that is checked here rather than assumed: this
    is the static half of "a pragma needs a check that it took effect".  The authoritative half is a
    csynth port list, and it belongs where a kernel actually carries a bram port (S4).
    """
    port = _bram_port("buf_w", 16, 1024, latency=1)
    assert port.decl == "ap_uint<16> buf_w[1024]"
    assert "*" not in port.decl, "an unsized pointer degrades to an ap_vld scalar port, silently"
    assert "mode=bram" in port.pragmas[0] and "port=buf_w" in port.pragmas[0]


# ---------------------------------------------------------------------------
# The check gate
# ---------------------------------------------------------------------------

def test_t2p_bram_lowers_to_rtl_module():
    assert check(T2pBram, RTL_MODULE) == (True, None)
    assert check(elaborate(T2pBram), RTL_MODULE) == (True, None)


def test_t2p_bram_does_not_lower_to_composite_kernel():
    """It is not a kernel, and the refusal says which hook it does declare."""
    ok, msg = check(T2pBram, "composite_kernel")
    assert ok is False
    assert "kernel_task()" in msg
    assert "rtl_module()" in msg, "the refusal should name the hook it DOES declare"


def test_the_hook_is_declared_by_identity_not_by_hasattr():
    """The trap that has already bitten once: the base defines the hook, so ``hasattr`` is True for
    every module including the ones that declare nothing."""
    assert hasattr(HwModule, "rtl_module")
    assert declares_hook(T2pBram, "rtl_module") is True
    assert declares_hook(HwModule, "rtl_module") is False


def test_a_module_that_declares_nothing_is_refused_by_the_hook_not_by_an_attribute_error():
    class _Bare(HwModule):
        pass

    ok, msg = check(_Bare, RTL_MODULE)
    assert ok is False
    assert "declares no rtl_module() hook" in msg


def test_an_unmappable_endpoint_kind_is_refused_by_name():
    """THE refusal.  A stream endpoint has no Verilog port mapping — and being told *that*, in those
    words, is the difference between a diagnosis and a ``KeyError`` from inside a walk."""

    class _StreamRtl(HwModule):
        def __post_init__(self) -> None:
            super().__post_init__()
            self.s_in = StreamIFSlave(sim=self.sim, name=f"{self.name}_s_in", bitwidth=32)
            self.add_endpoint(self.s_in)

        def rtl_module(self):
            return RtlModule(module="bram_t2p", files=("bram_t2p.v",), ports={"s_in": {}})

    ok, msg = check(_StreamRtl, RTL_MODULE)
    assert ok is False
    assert "StreamIFSlave has no Verilog port mapping" in msg
    assert "BramIFSlave" in msg, "the message should name the kinds that ARE mapped"
    with pytest.raises(LoweringError):
        rtl_port_mapping(StreamIFSlave(sim=ElabContext(), name="x", bitwidth=32))


def test_a_missing_file_is_the_declaration_being_false():
    class _Ghost(T2pBram):
        def rtl_module(self):
            return RtlModule(module="bram_t2p", files=("no_such_memory.v",),
                             ports=T2pBram.rtl_module(self).ports)

    ok, msg = check(_Ghost, RTL_MODULE)
    assert ok is False
    assert "no such file exists" in msg


def test_an_endpoint_missing_from_the_port_map_is_refused():
    class _HalfMapped(T2pBram):
        def rtl_module(self):
            rtl = T2pBram.rtl_module(self)
            return RtlModule(module=rtl.module, files=rtl.files,
                             ports={"wr_port": rtl.ports["wr_port"]}, params=rtl.params)

    ok, msg = check(_HalfMapped, RTL_MODULE)
    assert ok is False
    assert "rd_port" in msg


def test_a_missing_role_is_refused_by_role():
    class _NoWe(T2pBram):
        def rtl_module(self):
            rtl = T2pBram.rtl_module(self)
            wr = {k: v for k, v in rtl.ports["wr_port"].items() if k != "we"}
            return RtlModule(module=rtl.module, files=rtl.files,
                             ports={"wr_port": wr, "rd_port": rtl.ports["rd_port"]},
                             params=rtl.params)

    ok, msg = check(_NoWe, RTL_MODULE)
    assert ok is False
    assert "'we'" in msg and "wr_port" in msg


def test_a_port_the_verilog_does_not_declare_is_refused():
    """The two artifacts must agree on spelling, and that is checked rather than assumed."""

    class _Typo(T2pBram):
        def rtl_module(self):
            rtl = T2pBram.rtl_module(self)
            wr = dict(rtl.ports["wr_port"], addr="a_address")
            return RtlModule(module=rtl.module, files=rtl.files,
                             ports={"wr_port": wr, "rd_port": rtl.ports["rd_port"]},
                             params=rtl.params)

    ok, msg = check(_Typo, RTL_MODULE)
    assert ok is False
    assert "a_address" in msg and "does not declare" in msg


def test_a_module_name_absent_from_the_file_is_refused():
    class _WrongName(T2pBram):
        def rtl_module(self):
            rtl = T2pBram.rtl_module(self)
            return RtlModule(module="bram_tdp", files=rtl.files, ports=rtl.ports)

    ok, msg = check(_WrongName, RTL_MODULE)
    assert ok is False
    assert "bram_tdp" in msg


def test_verilog_port_parsing_reads_the_real_header():
    ports = verilog_module_ports((RTL_SRC_DIR / "bram_t2p.v").read_text(encoding="utf-8"),
                                 "bram_t2p")
    assert ports == ("clk", "a_addr", "a_en", "a_din", "a_we", "a_dout",
                     "b_addr", "b_en", "b_din", "b_we", "b_dout")


# ---------------------------------------------------------------------------
# The target vocabulary
# ---------------------------------------------------------------------------

def test_rtl_module_is_in_the_vocabulary_and_is_implemented():
    assert RTL_MODULE in ALL_TARGETS
    assert RTL_MODULE in IMPLEMENTED_TARGETS
    assert REALIZATION_HOOKS[RTL_MODULE] == "rtl_module"


def test_rtl_module_is_cut_independent_so_a_plain_hwmodule_can_be_asked():
    """A memory is hand-written RTL in a synthesized design and a ``FlatMemory`` BFM in an XSI
    testbench, with nothing about the memory changed.  Freezing that per-build role into
    ``potential_targets`` would be the ``ExtMod`` answer ``plans/design_cut.md`` rejected."""
    assert RTL_MODULE in CUT_INDEPENDENT_TARGETS
    from waveflow.build.codegen_check import potential_targets
    assert potential_targets(T2pBram) == frozenset(), "a plain HwModule claims no kind targets"
    assert check(T2pBram, RTL_MODULE) == (True, None)


def test_a_non_module_source_is_refused_for_a_per_module_target():
    ok, msg = check(int, RTL_MODULE)
    assert ok is False
    assert "not an HwModule" in msg


# ---------------------------------------------------------------------------
# Structure and footprint
# ---------------------------------------------------------------------------

def test_t2p_bram_carries_a_write_port_and_a_read_port():
    mem = elaborate(T2pBram)
    assert isinstance(mem.wr_port, BramIFSlave) and mem.wr_port.access == "write"
    assert isinstance(mem.rd_port, BramIFSlave) and mem.rd_port.access == "read"
    assert len(mem.endpoints) == 2


def test_a_port_used_both_ways_is_refused_at_construction():
    with pytest.raises(ValueError, match="access must be"):
        BramIFSlave(sim=ElabContext(), name="rw", element_type=word_element(16), nelem=1024,
                    access="rw")


def test_a_non_power_of_two_depth_is_refused_rather_than_rounded():
    with pytest.raises(ValueError, match="power of two"):
        elaborate(T2pBram, {"nelem": 1000}).addr_bits


def test_an_element_that_is_not_a_power_of_two_byte_count_is_refused_at_declaration():
    """The 14-bit RFdc sample, refused where the TYPE is named — not at wrapper generation.

    ``_bram_addr_shift`` cannot express Vitis's byte-address scaling for such a width, and that is a
    fact about the type: knowable the moment it is written down, so a design carrying one should not
    elaborate, simulate and csynth before dying at the last rung."""
    with pytest.raises(ValueError, match="cannot be a BRAM element type"):
        T2pBram(sim=ElabContext(), name="dense14", element_type=word_element(14))
    with pytest.raises(ValueError, match="cannot be a BRAM element type"):
        BramIFSlave(sim=ElabContext(), name="p24", element_type=word_element(24), access="read")


def test_the_width_is_derived_from_the_element_and_the_storage_is_typed():
    """``bitwidth`` is a consequence of what the memory holds, and pysim holds it as itself."""
    import numpy as np
    from waveflow.hw.dataschema import FloatField

    mem = elaborate(T2pBram, {"element_type": FloatField.specialize(bitwidth=32), "nelem": 256})
    assert mem.dwidth == 32 and mem.wr_port.bitwidth == 32
    assert mem.storage.dtype == np.dtype("float32"), (
        "a float memory holds floats, not a packing of them -- the precondition for a reference view")
    mem.store(3, 1.5)
    assert mem.load(3) == 1.5, "a float written is a float read, with no deserialize in between"


def test_the_footprint_is_declared_from_geometry():
    """1024x16 true-dual-port is one RAMB18, and no tool run is needed to know it."""
    assert ramb18_count(1024, 16) == 1
    assert ramb18_count(1024, 18) == 1
    assert ramb18_count(1024, 36) == 2          # too wide for one block: split
    assert ramb18_count(4096, 16) == 4          # too deep at 18 bits wide: stack
    assert ramb18_count(16384, 1) == 1          # the narrow aspect ratio is real storage

    mem = elaborate(T2pBram)
    mem.add_rm(None)
    assert mem.resource_model.predict(mem) == {"bram": 1, "uram": 0}


def test_the_declared_params_carry_the_geometry_to_the_instantiation():
    """Parameterizing is not generating: the file is copied, the numbers ride on the instance."""
    assert dict(elaborate(T2pBram).rtl_module().params) == {"DW": 16, "AW": 10}
    assert dict(elaborate(T2pBram, {"element_type": word_element(32), "nelem": 4096})
                .rtl_module().params) == {"DW": 32, "AW": 12}


def test_resolve_returns_the_descriptor_a_wrapper_emitter_will_need():
    rtl = resolve_rtl_module(elaborate(T2pBram))
    assert isinstance(rtl, RtlModule)
    assert rtl.module == "bram_t2p" and rtl.clock == "clk"
    assert rtl.ports["wr_port"]["dout"] == "a_dout"
