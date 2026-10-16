"""The packaging recipe and its readers, without Vivado (``plans/ip_packaging_routes.md`` Stage 3).
The IPs Vivado writes are gated in ``tests/examples/test_markov_ip.py``."""
from __future__ import annotations

from waveflow.build.system_ip import module_ports, package_tcl


def test_module_ports_reads_an_ansi_header():
    v = ("module dev (\n  input wire ap_clk,\n  input wire ap_rst_n,\n"
         "  output wire [31:0] m0_axi_AWADDR,\n  input  wire m0_axi_AWREADY,\n"
         "  output wire irq_q\n);\nendmodule\n")
    assert module_ports(v, "dev") == {"ap_clk": ("input", 1), "ap_rst_n": ("input", 1),
                                      "m0_axi_AWADDR": ("output", 32),
                                      "m0_axi_AWREADY": ("input", 1), "irq_q": ("output", 1)}


def test_the_recipe_packages_each_ip_and_wires_its_clock():
    tcl = package_tcl([{"module": "top_dev", "sources": [__file__], "root": "ip/top_dev",
                        "library": "top", "description": "a device", "interrupts": ["irq_q"]}],
                      part="xc7z020clg484-1")
    assert "ipx::package_project" in tcl and "-vendor waveflow.org -library top" in tcl
    assert "ipx::infer_bus_interface irq_q xilinx.com:signal:interrupt_rtl:1.0" in tcl
    assert "ASSOCIATED_BUSIF" in tcl and "ASSOCIATED_RESET" in tcl
    assert tcl.count("create_project -in_memory") == 1 and tcl.rstrip().endswith('puts "WF_DONE"')
