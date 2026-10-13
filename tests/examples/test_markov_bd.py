"""markov's block design: rung 4 of ``plans/board_packaging.md`` for
``plans/ip_packaging_routes.md`` Stage 4 -- its packaged IPs instantiated beside AMD's crossbar,
wired from the spec the XSI system top is rendered from, and ``validate_bd_design`` passing.

Everything is read back from Vivado after validation (``block_design.json``), and each check holds
the design against the system top the XSI gates run: the same ports, the same links, the same
address map, the same crossbar.

Needs Vivado and markov's csynth'd kernels (``synth="check"``: stale or missing RTL fails the run).
"""
from __future__ import annotations

import json

import pytest

from examples.markov.markov_build import HERE, TOP, WORKSPACE, build_dag, system
from tests.examples._system_run import WORK, system_run
from waveflow.build.system_ip import module_ports
from waveflow.build.system_top import XBAR_INST, SystemTopSpec

pytestmark = pytest.mark.xsi


@pytest.fixture(scope="module")
def bd() -> dict:
    work = system_run(build_dag(work_dir=WORK), HERE, WORKSPACE, through="block_design")
    return json.loads((work / "bd" / "block_design.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def spec() -> SystemTopSpec:
    return SystemTopSpec.from_system(system(), top=TOP)


def test_the_design_validates_with_no_critical_warning(bd):
    assert bd["bd"].endswith("markov_top_bd.bd") and bd["critical"] == []


def test_a_bitstream_build_will_synthesize_it_globally(bd):
    """The default synthesis mode (Stage 5): one global run, not Vivado's per-IP default."""
    assert bd["synth_checkpoint_mode"] == "None"


def test_the_wrapper_has_the_system_tops_ports(bd, spec):
    """The block design is a drop-in for the system top: the same ports, directions and widths
    (``make_wrapper`` writes an AXI group's names in lower case)."""
    top = module_ports(spec.render_verilog(), spec.top)
    assert {n.lower(): tuple(v) for n, v in bd["ports"].items()} == \
        {n.lower(): v for n, v in top.items()}


def test_the_links_are_the_system_tops(bd):
    """Each device meets the crossbar on the slots it does in the system top; the host's bus and
    the interrupt lines leave the design."""
    want = [["/s0_axi", f"/{XBAR_INST}/S00_AXI"],
            ["/u_gen_sub/si1_axi", f"/{XBAR_INST}/S01_AXI"],
            ["/u_chain_sub/si2_axi", f"/{XBAR_INST}/S02_AXI"],
            ["/u_chain_sub/si3_axi", f"/{XBAR_INST}/S03_AXI"],
            ["/u_gen_sub/mi0_axi", f"/{XBAR_INST}/M00_AXI"],
            ["/u_chain_sub/mi1_axi", f"/{XBAR_INST}/M01_AXI"],
            ["/u_mem_sub/mi2_axi", f"/{XBAR_INST}/M02_AXI"]]
    assert sorted(bd["intf_nets"]) == sorted(want)
    nets = {tuple(v) for v in bd["nets"].values()}
    assert ("/irq_gen_qcmd", "/u_gen_sub/irq_gen_qcmd") in nets
    assert ("/irq_chain_qresp", "/u_chain_sub/irq_chain_qresp") in nets
    cells = ["/u_chain_sub", "/u_gen_sub", "/u_mem_sub"]
    assert ("/ap_clk", *[c + "/ap_clk" for c in cells], f"/{XBAR_INST}/aclk") in nets
    assert ("/ap_rst_n", *[c + "/ap_rst_n" for c in cells], f"/{XBAR_INST}/aresetn") in nets


def test_every_master_sees_every_slave_at_its_crossbar_window(bd, spec):
    """The address map is written once, in Python: each of the four masters (the host and the three
    bus writers) maps each slave at the crossbar slot's base and size, not Vivado's own choice."""
    slave = {0: "/u_gen_sub/mi0_axi/reg0", 1: "/u_chain_sub/mi1_axi/reg0",
             2: "/u_mem_sub/mi2_axi/reg0"}
    want = {slave[k]: [r.base, r.size] for k, r in enumerate(spec.xbar.mi)}
    assert bd["segments"] == {sp: want for sp in ("/s0_axi", "/u_gen_sub/si1_axi",
                                                  "/u_chain_sub/si2_axi", "/u_chain_sub/si3_axi")}


def test_the_crossbar_is_the_xsi_crossbar(bd, spec):
    """After validation (and its parameter propagation) the crossbar cell holds the configuration
    the XSI system top's crossbar was generated with."""
    assert bd["xbar_props"] == spec.xbar.config_props()


def test_an_unchanged_system_starts_no_vivado(bd):
    work = system_run(build_dag(work_dir=WORK), HERE, WORKSPACE, through="block_design")
    again = json.loads((work / "bd" / "block_design.json").read_text(encoding="utf-8"))
    assert again["key"] == bd["key"]
    from waveflow.build.system_bd import BlockDesign, build_block_design
    from waveflow.build.system_ip import SystemIp

    sysip = SystemIp.from_json(
        json.loads((work / "ip_repo" / "manifest.json").read_text(encoding="utf-8")))
    res = build_block_design(SystemTopSpec.from_system(system(), top=TOP), sysip,
                             period_ns=bd["period_ns"], out_dir=work / "bd")
    assert isinstance(res, BlockDesign) and res.cached and res.seconds == 0.0
