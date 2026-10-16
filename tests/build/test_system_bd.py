"""The block-design recipe, without Vivado (``plans/ip_packaging_routes.md`` Stage 4).  The design
Vivado builds is gated in ``tests/examples/test_markov_bd.py``."""
from __future__ import annotations

from pathlib import Path

import pytest

from waveflow.build.system_bd import BdLink, bd_links, bd_tcl, master_spaces, wrapper_ports
from waveflow.build.system_ip import BusInterface, PackagedIp


def _markov():
    from examples.markov.markov_build import TOP, system
    from waveflow.build.system_top import SystemTopSpec

    return SystemTopSpec.from_system(system(), top=TOP)


def _ip(sub: str, *busifs) -> PackagedIp:
    """An IP as its component.xml would read: the named interfaces plus clock and reset."""
    ifs = [BusInterface(n, bus, mode) for n, bus, mode in busifs]
    ifs += [BusInterface("ap_clk", "clock", "slave"), BusInterface("ap_rst_n", "reset", "slave")]
    return PackagedIp(sub=sub, module=f"markov_top_{sub}",
                      vlnv=f"waveflow.org:markov_top:{sub}:1.0", root=Path("repo") / sub,
                      interfaces=ifs)


MARKOV_IPS = {
    "gen": _ip("gen", ("mi0_axi", "aximm", "slave"), ("si1_axi", "aximm", "master"),
               ("irq_gen_qcmd", "interrupt", "master")),
    "chain": _ip("chain", ("mi1_axi", "aximm", "slave"), ("si2_axi", "aximm", "master"),
                 ("si3_axi", "aximm", "master"), ("irq_chain_qresp", "interrupt", "master")),
    "mem": _ip("mem", ("mi2_axi", "aximm", "slave")),
}


def test_markovs_links_are_found_by_name():
    assert bd_links(_markov(), MARKOV_IPS) == [
        BdLink("port", "u_xbar/S00_AXI", port="s0_axi"),
        BdLink("intf", "u_gen_sub/si1_axi", "u_xbar/S01_AXI"),
        BdLink("intf", "u_chain_sub/si2_axi", "u_xbar/S02_AXI"),
        BdLink("intf", "u_chain_sub/si3_axi", "u_xbar/S03_AXI"),
        BdLink("intf", "u_xbar/M00_AXI", "u_gen_sub/mi0_axi"),
        BdLink("intf", "u_xbar/M01_AXI", "u_chain_sub/mi1_axi"),
        BdLink("intf", "u_xbar/M02_AXI", "u_mem_sub/mi2_axi"),
        BdLink("pin", "u_gen_sub/irq_gen_qcmd", port="irq_gen_qcmd"),
        BdLink("pin", "u_chain_sub/irq_chain_qresp", port="irq_chain_qresp"),
    ]


def test_a_crossbar_slot_must_meet_exactly_one_ip():
    ips = dict(MARKOV_IPS, mem=_ip("mem"))          # the memory lost its slave interface
    with pytest.raises(ValueError, match="mi2_axi meets 0"):
        bd_links(_markov(), ips)


def test_the_address_map_is_assigned_from_the_crossbar():
    spec = _markov()
    links = bd_links(spec, MARKOV_IPS)
    assert master_spaces(spec, links) == ["/s0_axi", "/u_gen_sub/si1_axi", "/u_chain_sub/si2_axi",
                                          "/u_chain_sub/si3_axi"]
    tcl = bd_tcl(spec, MARKOV_IPS, name="markov_top_bd", part="xc7z020clg484-1", period_ns=10.0,
                 repo=Path("repo"), proj=Path("proj"), out=Path("out"), links=links)
    # 4 masters x 3 slaves, each at its crossbar window -- never left to assign_bd_address alone.
    assert tcl.count("assign_bd_address -offset") == 12
    assert ("assign_bd_address -offset 0x00100000 -range 4K -target_address_space "
            "[get_bd_addr_spaces /u_chain_sub/si3_axi] "
            "[get_bd_addr_segs -of_objects [get_bd_intf_pins u_mem_sub/mi2_axi]]") in tcl
    assert "CONFIG.M01_A00_BASE_ADDR {0x0000000000004000}" in tcl
    assert "-freq_hz 100000000 ap_clk" in tcl and "validate_bd_design" in tcl
    assert "set_property synth_checkpoint_mode None" in tcl         # global by default
    assert tcl.rstrip().endswith('puts "WF_DONE"')


def test_wrapper_ports_reads_non_ansi_declarations():
    v = ("module t_wrapper\n   (ap_clk,\n    s0_axi_araddr,\n    irq);\n  input ap_clk;\n"
         "  input [31:0]s0_axi_araddr;\n  output irq;\n")
    assert wrapper_ports(v) == {"ap_clk": ("input", 1), "s0_axi_araddr": ("input", 32),
                                "irq": ("output", 1)}
