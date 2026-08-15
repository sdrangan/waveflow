"""S2's wiring, checked without a toolchain — ``plans/rtl_module.md``.

Two claims are worth separating, because only the first is cheap:

* **The mechanism** — a ``BramIF`` is not an ``add_if`` channel, so the accessor's port stays a
  *boundary port* of the kernel and the join happens in the wrapper.  That is structural and is
  checked here, in milliseconds.
* **The design works** — the elaborated wrapper returns the witness's five values.  Nothing static
  can say that; it is ``tests/examples/test_bram_toy_xsi.py``, and it needs Vivado.

The wrapper's shape is gated against ``plans/witness/t2p_bram/rx_top.v``, which was hand-written and
simulated: same instantiation, same A-half wiring, same B-half tie-offs.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from waveflow.build.composite_gen import (
    bram_port_signals,
    composite_top_spec,
    render_ports_h,
    tb_top_spec,
    wrapper_name,
)
from waveflow.build.elaborate import elaborate
from waveflow.build.hwcodegen import LoweringError
from waveflow.build.wrapper_gen import render_wrapper, wrapper_spec
from waveflow.hw.bram import BramIF, BramIFMaster, T2pBram

from examples.bram_toy.bram_toy import DEPTH, FILL, WORD_BW, BramToy, BramToyTB

WITNESS = Path(__file__).resolve().parents[2] / "plans" / "witness" / "t2p_bram"
_ELAB = {"bitwidth": WORD_BW, "depth": DEPTH, "fill": FILL}


def _dut():
    return elaborate(BramToy, dict(_ELAB), name="bram_toy")


def _spec():
    comp = _dut()
    return comp, composite_top_spec(comp, width=WORD_BW)


# ---------------------------------------------------------------------------
# The mechanism: a wrapper wire is not a channel
# ---------------------------------------------------------------------------

def test_a_bram_port_becomes_a_boundary_port_with_no_change_to_derive_boundary():
    """THE claim S2 rests on.  ``derive_boundary`` was not taught anything about memories: the
    accessor's port is simply not bound to one of the composite's ``add_if`` interfaces, so the
    existing rule — *a child endpoint not bound to an internal interface IS a boundary port* — makes
    it one."""
    comp = _dut()
    names = [n for n, _ep in comp.boundary]
    assert names == ["rx_str", "buf_w", "addr_str", "out_str", "buf_r"]
    kinds = {n: type(ep).__name__ for n, ep in comp.boundary}
    assert kinds["buf_w"] == "BramIFMaster" and kinds["buf_r"] == "BramIFMaster"


def test_the_go_channel_is_an_add_if_edge_and_leaves_the_boundary():
    """The contrast that makes the point: the same graph has one of each, and they behave
    differently *because of which registry they are in*, not because of their type."""
    comp = _dut()
    assert [c.name for c in comp.internal_edges] == ["go"]
    assert "go_out" not in [n for n, _ in comp.boundary]
    assert len(comp.rtl_ifs) == 2 and len(comp.interfaces) == 1


def test_the_memory_is_not_a_task():
    """It is in ``rtl_mods``, so no walk that emits ``hls::task``\\ s ever sees it — which is why
    nothing asks a memory for a ``kernel_task()`` it does not have."""
    comp = _dut()
    assert [type(m).__name__ for m in comp.rtl_mods.values()] == ["T2pBram"]
    assert [type(c).__name__ for c in comp.ordered_subcomps] == ["BramWrite", "BramRead"]
    assert all(type(c).__name__ != "T2pBram" for c in comp.sub_comps.values())


def test_the_kernel_carries_the_bram_ports_with_a_sized_array_and_the_memorys_latency():
    comp, spec = _spec()
    bram = [p for p in spec.ports if p.kind == "bram"]
    assert [p.name for p in bram] == ["buf_w", "buf_r"]
    for p in bram:
        assert p.decl == f"ap_uint<16> {p.name}[1024]", "sized array, never a pointer"
        assert p.pragmas == (
            f"#pragma HLS INTERFACE mode=bram port={p.name} storage_type=ram_1wnr latency=1",)
    # The 1 in `latency=1` is the memory's, read from its Verilog -- not a number in any Python file.
    assert comp.mem.read_latency == 1


# ---------------------------------------------------------------------------
# The wrapper, against the witness
# ---------------------------------------------------------------------------

def _witness_kernel_conns() -> dict[str, str]:
    """``{port: expression}`` for the witness's kernel instantiation."""
    top = (WITNESS / "rx_top.v").read_text(encoding="utf-8")
    inst = top[top.index("rx kernel ("):]
    inst = inst[:inst.index(");")]
    return {m.group(1): m.group(2).strip() for m in re.finditer(r"\.(\w+)\s*\(([^)]*)\)", inst)}


def test_the_wrapper_connects_every_bram_net_the_witness_does():
    """Same 28 nets, and the same **electrical** shape.

    One deliberate difference in spelling, recorded here rather than smoothed over: the witness
    declares wires for the B half and connects them to nothing (``.buf_w_Addr_B(bw_addr_b)``, with
    ``bw_addr_b`` never read); the emitter leaves those kernel *outputs* open
    (``.buf_w_Addr_B()``).  Both are "unused"; the emitter's form does not declare a wire nobody
    reads.  What must match exactly is the part that carries data — the A half — and the B half's
    ``Dout``, which is a kernel INPUT and so must be *driven* in both.
    """
    comp, spec = _spec()
    w = wrapper_spec(comp, spec)
    conns = dict(w.kernel_conns)
    witness = _witness_kernel_conns()
    ties = dict(w.tieoffs)

    for port in ("buf_w", "buf_r"):
        for sig, net in bram_port_signals(port).items():
            assert net in conns, f"the wrapper leaves {net} unconnected"
            theirs = witness[f"{port}_{sig}"]
            if sig in ("Clk_A", "Clk_B", "Rst_A", "Rst_B"):
                assert conns[net] == "" and theirs == "", f"{sig}: open in both"
            elif sig.endswith("_A"):
                assert conns[net] and theirs, f"{sig}: the A half carries data and must be wired"
            elif sig == "Dout_B":
                assert conns[net] in ties and witness[f"{port}_Dout_B"], (
                    "Dout_B is a kernel INPUT: undriven, it carries an X into the design")
            else:
                assert conns[net] == "", f"{sig}: an unused kernel output should be left open"


def test_the_a_half_reaches_the_memory_and_the_b_half_is_tied_off():
    comp, spec = _spec()
    w = wrapper_spec(comp, spec)
    assert len(w.mems) == 1
    mem = w.mems[0]
    assert (mem.module, mem.inst, mem.clock) == ("bram_t2p", "mem", "clk")
    assert mem.params == (("DW", 16), ("AW", 10)), "the witness's #(.DW(16), .AW(10))"

    conns = dict(mem.conns)
    # The write accessor drives port A, the read accessor port B -- one memory, two logical ports.
    assert conns["a_addr"] == "buf_w_addr_a" and conns["a_din"] == "buf_w_din_a"
    assert conns["b_addr"] == "buf_r_addr_a" and conns["b_dout"] == "buf_r_dout_a"
    assert dict(w.tieoffs) == {"buf_w_dout_b": "16'd0", "buf_r_dout_b": "16'd0"}


def test_the_wrappers_pins_are_axi_stream_and_nothing_else():
    """What makes S3 small: the memory is internal, so the testbench sees only AXIS — no BFM."""
    comp, spec = _spec()
    w = wrapper_spec(comp, spec)
    names = [n for n, _d, _w in w.ports]
    assert names == ["ap_clk", "ap_rst_n",
                     "rx_str_TDATA", "rx_str_TVALID", "rx_str_TREADY",
                     "addr_str_TDATA", "addr_str_TVALID", "addr_str_TREADY",
                     "out_str_TDATA", "out_str_TVALID", "out_str_TREADY"]
    assert not any("buf" in n for n in names)


def test_the_rendered_wrapper_is_verilog_the_witness_would_recognize():
    comp, spec = _spec()
    text = render_wrapper(wrapper_spec(comp, spec))
    assert "module bram_toy_top (" in text
    assert "bram_toy kernel (" in text
    assert "bram_t2p #(.DW(16), .AW(10)) mem (" in text
    assert "assign buf_w_dout_b = 16'd0;" in text
    assert text.count(".buf_w_") == 14 and text.count(".buf_r_") == 14


def test_the_committed_wrapper_matches_what_the_generator_emits():
    """The committed artifact is a build output, and a build output nobody checks drifts."""
    from examples.bram_toy.bram_toy_build import wrapper_text

    committed = (Path(__file__).resolve().parents[2] / "examples" / "bram_toy" / "xsi" /
                 "bram_toy_top.v")
    assert committed.read_text(encoding="utf-8").replace("\r\n", "\n") == wrapper_text(), (
        "examples/bram_toy/xsi/bram_toy_top.v has drifted — regenerate it "
        "(bram_toy_build.py --through codegen_dut)")


# ---------------------------------------------------------------------------
# The elaborated top is the wrapper
# ---------------------------------------------------------------------------

def test_the_ports_header_names_the_wrapper_and_hides_the_bram_ports():
    _comp, spec = _spec()
    assert spec.rtl_top == wrapper_name("bram_toy") == "bram_toy_top"
    assert spec.elab_top == "bram_toy_top"
    h = render_ports_h(spec)
    assert 'TOP        = "bram_toy_top"' in h
    assert 'xsim.dir/bram_toy_top/xsimk' in h
    assert "buf_w" not in h and "buf_r" not in h, (
        "a bram port is not a pin on the elaborated design — a testbench binding to it would be "
        "driving a wire that does not exist on the module it loaded")


def test_an_unwrapped_design_is_unchanged():
    """No existing design declares an RTL module, so ``rtl_top`` is None and every emitted byte is
    what it was."""
    from examples.mem_copy.mem_copy import MemCopy

    spec = composite_top_spec(elaborate(MemCopy), width=64)
    assert spec.rtl_top is None and spec.elab_top == spec.top_name
    assert spec.pin_ports == spec.ports


def test_the_testbench_has_a_model_per_pin_and_none_for_the_memory():
    """If a memory ever needed a BFM, the wrapper would be the thing that is wrong."""
    from waveflow.simulation.simulation import Simulation

    spec = tb_top_spec(BramToyTB(name="tb", sim=Simulation()))
    assert [m.cls for m in spec.models] == ["AxisMaster", "AxisMaster", "AxisSlave"]
    assert not any("Bram" in m.cls or "Mem" in m.cls for m in spec.models)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------

def test_a_bram_port_with_no_memory_cannot_be_lowered():
    """An unbound accessor has no latency to emit, and inventing one is what shifts the ramp."""
    from waveflow.build.elaborate import ElabContext

    ep = BramIFMaster(sim=ElabContext(), name="loose", bitwidth=16, depth=1024, access="read")
    with pytest.raises(ValueError, match="not bound to a BramIF"):
        _ = ep.read_latency


def test_a_geometry_mismatch_is_refused_at_bind_time():
    from waveflow.build.elaborate import ElabContext

    sim = ElabContext()
    mem = T2pBram(sim=sim, name="m", dwidth=16, depth=1024)
    small = BramIFMaster(sim=sim, name="small", bitwidth=16, depth=256, access="write")
    iface = BramIF(name="bad", sim=sim)
    iface.bind(ep_name="master", endpoint=small)
    with pytest.raises(ValueError, match="256x16 but the memory port is 1024x16"):
        iface.bind(ep_name="slave", endpoint=mem.wr_port)


def test_a_direction_mismatch_is_refused_at_bind_time():
    from waveflow.build.elaborate import ElabContext

    sim = ElabContext()
    mem = T2pBram(sim=sim, name="m", dwidth=16, depth=1024)
    reader = BramIFMaster(sim=sim, name="rd", bitwidth=16, depth=1024, access="read")
    iface = BramIF(name="bad", sim=sim)
    iface.bind(ep_name="master", endpoint=reader)
    with pytest.raises(ValueError, match="read-during-write collision"):
        iface.bind(ep_name="slave", endpoint=mem.wr_port)      # the WRITE port


def test_add_rtl_mod_refuses_a_module_with_no_verilog():
    from waveflow.build.elaborate import ElabContext
    from waveflow.hw.hw_module import HwModule

    class _Plain(HwModule):
        pass

    sim = ElabContext()
    parent, child = _Plain(name="p", sim=sim), _Plain(name="c", sim=sim)
    with pytest.raises(TypeError, match="declares no rtl_module"):
        parent.add_rtl_mod(child)


def test_a_verilog_keyword_instance_name_is_refused_by_name():
    """``self.buf`` is the most natural name for a buffer and is a Verilog PRIMITIVE GATE.  The
    emitter refuses it rather than letting xvlog fail on a syntax error that mentions no Python."""
    from waveflow.build.wrapper_gen import _inst_name

    class _Holder:
        pass

    holder, mem = _Holder(), object()
    holder.buf = mem
    with pytest.raises(LoweringError, match="Verilog keyword"):
        _inst_name(holder, mem)


def test_render_rtl_f_appends_the_wrappers_own_sources_last():
    root = Path(__file__).resolve().parents[2] / "examples" / "bram_toy"
    if not (root / "bram_toy_proj" / "solution1" / "syn" / "verilog").is_dir():
        pytest.skip("no csynth RTL for bram_toy")
    from waveflow.build.composite_gen import render_rtl_f

    lines = render_rtl_f("bram_toy", root, extra=("bram_t2p.v", "bram_toy_top.v")).splitlines()
    assert lines[-2:] == ["bram_t2p.v", "bram_toy_top.v"]
    assert all(ln.startswith("../bram_toy_proj/") for ln in lines[:-2])
    assert len(lines) >= 6, "a .f naming only the top does not elaborate — all csynth files are needed"
