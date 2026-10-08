"""``waveflow/build/system_top.py`` -- a system's RTL top, walked from its pysim graph
(``plans/xsi_system_top.md`` Stage 3).

The oracle for the generated tops is the XSI gates (``tests/examples/test_mm_fir_xsi.py``: 618 / 611,
``test_markov_xsi.py``: 1870, bit-exact), which run them.  These tests are the fast half: what the walk
derives from the graph, the tie-off rules in the rendered text, the refusals -- and, when the csynth'd
RTL is present, that the pins derived from each kernel's ``TopSpec`` are exactly its module's ports.
"""
from __future__ import annotations

import re

import pytest

from waveflow.build.hwcodegen import LoweringError
from waveflow.build.system_top import (
    default_id_width,
    kernel_pins,
    render_system_top,
    system_top_spec,
)


def _fir(topology: str):
    from examples.mm_fir.mm_fir import MmFirSystem
    from examples.mm_fir.mm_fir_xsi import PLAN
    sysm = MmFirSystem(x=[0], plan=PLAN, one_front=topology == "one_front")
    return sysm, system_top_spec(sysm.xbar, [sysm.fir], top="mm_fir_top")


def _markov():
    from examples.markov.markov_xsi import system
    sysm = system()
    return sysm, system_top_spec(sysm.xbar, [sysm.gen, sysm.chain, sysm.mem], top="markov_top")


def test_id_width_routes_every_master():
    assert [default_id_width(n) for n in (1, 2, 3, 4, 5)] == [1, 1, 2, 2, 3]


@pytest.mark.parametrize("topology", ["per_view", "one_front"])
def test_mm_fir_the_host_is_the_only_port(topology):
    _sysm, spec = _fir(topology)
    assert spec.host_axi == ("s0_axi",)
    assert spec.irqs == (("irq_qin", "qin"), ("irq_qout", "qout"), ("irq_qresp", "qresp"))
    assert spec.modules == ("mm_fir",)
    kinds = [m.kind for m in spec.mi]
    assert kinds == (["view"] * 4 if topology == "per_view" else ["adaptor", "stub"])
    assert all(n.fifo_depth == 0 for n in spec.nets), "a view's link IS its FIFO"


def test_mm_fir_tie_offs_are_the_rules():
    """The kernel's queue-out and status ports carry no TLAST; the leaves that read one see 0.  The
    config and queue-in leaves drive TLAST into a kernel that ignores it: nothing to tie."""
    _sysm, spec = _fir("per_view")
    v = render_system_top(spec)
    tied = sorted(re.findall(r"assign (\w+)_TLAST = 1'b0;", v))
    assert tied == ["k_qout", "k_qresp", "k_regs_stat"]


def test_markov_the_cut_brings_devices_and_writers():
    sysm, spec = _markov()
    assert spec.si == (("s0_axi", True), ("si1_axi", False), ("si2_axi", False), ("si3_axi", False))
    assert spec.xbar.id_width == 2
    assert spec.modules == ("markov_gen", "markov_chain", "mm_queue_writer_64_128",
                            "mm_credit_writer_64")
    assert [m.kind for m in spec.mi] == ["adaptor", "adaptor", "memory"]
    assert spec.idle_memory_ports == ("mem_b",)
    # The forward link between the generator and its writer is a FIFO at the link's declared depth;
    # every other link joins a kernel to a view (the view is the FIFO) or is at the default depth.
    fifos = {n.name: n.fifo_depth for n in spec.nets if n.fifo_depth}
    assert fifos == {"u_fwd": int(sysm.u_link.fwd_depth)}
    # Each writer's target is its view's bus WORD index, from the address map.
    targets = {k.module: dict((n, v) for n, _w, v in k.scalars).get("target") for k in spec.kernels}
    bpw = 8
    assert targets["mm_queue_writer_64_128"] == sysm.u_link.qin.base // bpw
    assert targets["mm_credit_writer_64"] == sysm.u_link.crd_in.base // bpw


def test_markov_rendered_rules():
    _sysm, spec = _markov()
    v = render_system_top(spec)
    assert "assign chain_k_qresp_TLAST = 1'b0;" in v          # unframed response port
    assert "assign u_fwd_q_TKEEP = {8{1'b1}};" in v            # past the FIFO: every byte valid
    assert ".m_axi_gmem0_BID(si3_axi_BID[0])" in v             # a 1-bit Vitis ID on a 2-bit SI
    assert ".s_axi_control_AWVALID(0)" in v
    assert ".target(32'd" in v


def test_a_memory_outside_the_cut_is_refused():
    sysm, _spec = _markov()
    with pytest.raises(LoweringError, match="not inside the cut"):
        system_top_spec(sysm.xbar, [sysm.gen, sysm.chain])


def test_a_device_whose_kernel_is_outside_is_refused():
    sysm, _spec = _markov()
    with pytest.raises(LoweringError, match="not the device of a kernel inside the cut"):
        system_top_spec(sysm.xbar, [sysm.chain, sysm.mem])


def _csynth_ports(rtl_dir, top):
    """``(direction, name)`` of every port of the csynth'd module -- read from the Verilog, here and
    only here: the test that the spec-derived pins are the real ones."""
    path = rtl_dir / f"{top}.v"
    if not path.is_file():
        return None
    src = path.read_text(encoding="utf-8", errors="replace")
    body = src[src.index(f"module {top}"):]
    body = body[:body.index("endmodule")]
    return sorted((m[1], m[2]) for m in re.finditer(
        r"^\s*(input|output)\s+(?:wire\s+)?(?:\[[^\]]*\]\s*)?(\w+)\s*;", body, re.M))


@pytest.mark.parametrize("system", ["markov", "mm_fir"])
def test_derived_pins_are_the_csynth_modules_ports(system):
    if system == "markov":
        from examples.markov.markov_xsi import rtl_dir
        _sysm, spec = _markov()
    else:
        from examples.mm_fir.mm_fir_xsi import RTL
        _sysm, spec = _fir("per_view")
        rtl_dir = lambda _t: RTL                       # noqa: E731
    checked = 0
    for k in spec.kernels:
        want = _csynth_ports(rtl_dir(k.module), k.module)
        if want is None:
            continue
        assert sorted(kernel_pins(k)) == want, k.module
        checked += 1
    if not checked:
        pytest.skip(f"no csynth RTL for {system} -- run its *_build")


# ---------------------------------------------------------------------------------------------
# Timing probes are named by the pysim object they watch; the spec resolves the net.
# ---------------------------------------------------------------------------------------------

def test_probes_resolve_from_pysim_objects():
    from examples.markov.markov_xsi import timing_probes
    from waveflow.build.system_top import beat, last, stall

    sysm, spec = _markov()
    got = {n: spec.probe_expr(p) for n, p in timing_probes(sysm).items()}
    assert got["cmd"] == "gen_k_qcmd_TVALID && gen_k_qcmd_TREADY"      # a view -> kernel net
    assert got["ufwd_last"] == "u_fwd_TVALID && u_fwd_TREADY && u_fwd_TLAST"
    assert got["wr3_b"] == "si3_axi_BVALID && si3_axi_BREADY"           # a bus master's SI slot
    # The writer's side of the FIFO'd link is the consumer net.
    assert spec.probe_expr(beat(sysm.u_link.fwd_writer.s_in)) == "u_fwd_q_TVALID && u_fwd_q_TREADY"
    assert spec.probe_expr(stall(sysm.chain.m_resp)) == \
        "chain_k_qresp_TVALID && !chain_k_qresp_TREADY"
    # The host's own master is outside the cut, but it is still a crossbar slot: the top's port.
    assert spec.probe_expr(beat(sysm.host.m, "AR")) == "s0_axi_ARVALID && s0_axi_ARREADY"
    assert spec.probe_expr(last(sysm.chain.m_mem, "W")).endswith("si3_axi_WLAST")
    assert "probe_cmd" in render_system_top(spec, timing_probes(sysm))


def test_a_probe_that_names_nothing_in_the_top_is_refused():
    from waveflow.build.system_top import beat

    sysm, spec = _markov()
    with pytest.raises(LoweringError, match="AXI channel"):
        spec.probe_expr(beat(sysm.chain.m_mem))                        # a master needs a channel
    with pytest.raises(LoweringError, match="not a stream endpoint"):
        spec.probe_expr(beat(sysm.host.irq_qcmd))                      # not on any net
