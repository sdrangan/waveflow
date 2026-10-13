"""markov packaged as IP, one per device: rung 3 of ``plans/board_packaging.md`` for
``plans/ip_packaging_routes.md`` Stage 3 -- each IP has the expected VLNV, ports and interfaces.

Four IPs: the generator device, the chain device, the memory (each packaged by ``ip_package`` from
what its sub-top compiles) and AMD's crossbar (its ``.xci``, as is).  Everything is read back from
the IPs Vivado wrote, not from what the script asked for.  Every link between them is AXI4 or an
interrupt line, so the memory-mapped wiring needs no custom interface definition.

Needs Vivado and markov's csynth'd kernels (``synth="check"``: stale or missing RTL fails the run).
"""
from __future__ import annotations

import json

import pytest

from examples.markov.markov_build import HERE, TOP, WORKSPACE, build_dag
from tests.examples._system_run import WORK, system_run

pytestmark = pytest.mark.xsi

#: sub-top -> (VLNV, {interface: (bus, mode)}).  The AXI4 groups are named for the crossbar slot
#: they meet: ``mi<k>_axi`` faces a crossbar master port (so the device is the slave), ``si<k>_axi``
#: a slave port (the device is a master: its bus writers and the chain's memory writer).
WANT = {
    "gen": ("waveflow.org:markov_top:markov_top_gen:1.0",
            {"mi0_axi": ("aximm", "slave"), "si1_axi": ("aximm", "master"),
             "irq_gen_qcmd": ("interrupt", "master"),
             "ap_clk": ("clock", "slave"), "ap_rst_n": ("reset", "slave")}),
    "chain": ("waveflow.org:markov_top:markov_top_chain:1.0",
              {"mi1_axi": ("aximm", "slave"), "si2_axi": ("aximm", "master"),
               "si3_axi": ("aximm", "master"), "irq_chain_qresp": ("interrupt", "master"),
               "ap_clk": ("clock", "slave"), "ap_rst_n": ("reset", "slave")}),
    "mem": ("waveflow.org:markov_top:markov_top_mem:1.0",
            {"mi2_axi": ("aximm", "slave"),
             "ap_clk": ("clock", "slave"), "ap_rst_n": ("reset", "slave")}),
}


@pytest.fixture(scope="module")
def manifest() -> dict:
    work = system_run(build_dag(work_dir=WORK), HERE, WORKSPACE, through="ip_package")
    return json.loads((work / "ip_repo" / "manifest.json").read_text(encoding="utf-8"))


def test_four_ips_with_the_expected_vlnvs(manifest):
    got = {n: ip["vlnv"] for n, ip in manifest["ips"].items()}
    assert got == {n: w[0] for n, w in WANT.items()}
    assert manifest["xbar"]["vlnv"] == "xilinx.com:ip:axi_crossbar:2.1"


def test_each_ip_has_exactly_the_expected_interfaces(manifest):
    for name, (_vlnv, want) in WANT.items():
        got = {b["name"]: (b["bus"], b["mode"]) for b in manifest["ips"][name]["interfaces"]}
        assert got == want, name


def test_every_port_is_in_an_interface(manifest):
    """No loose wire: each port belongs to an AXI group, or is the clock, the reset or an
    interrupt -- what a block design can connect without hand-wiring."""
    for name, ip in manifest["ips"].items():
        groups = [b["name"] for b in ip["interfaces"] if b["bus"] == "aximm"]
        singles = {b["name"] for b in ip["interfaces"] if b["bus"] != "aximm"}
        loose = [p for p in ip["ports"]
                 if p not in singles and not any(p.startswith(g + "_") for g in groups)]
        assert loose == [], (name, loose)


def test_the_clock_drives_every_axi_interface_and_the_reset(manifest):
    for name, ip in manifest["ips"].items():
        clk = next(b for b in ip["interfaces"] if b["name"] == "ap_clk")
        axi = sorted(b["name"] for b in ip["interfaces"] if b["bus"] == "aximm")
        assert sorted(clk["params"]["ASSOCIATED_BUSIF"].split(":")) == axi, name
        assert clk["params"]["ASSOCIATED_RESET"] == "ap_rst_n", name


def test_an_unchanged_system_is_not_repackaged(manifest):
    """Each IP is cached by the content of what it reads: a second run of the step starts no
    Vivado, and writes the same manifest (crossbar included)."""
    work = system_run(build_dag(work_dir=WORK), HERE, WORKSPACE, through="ip_package")
    again = json.loads((work / "ip_repo" / "manifest.json").read_text(encoding="utf-8"))
    assert again["seconds"] == 0.0
    assert again["ips"] == manifest["ips"] and again["xbar"] == manifest["xbar"]


def test_each_ip_carries_exactly_what_the_xsi_gates_verified(manifest):
    """The IP is the verified design, not a re-derivation of it: its files are byte for byte the
    sub-top's compile list (its kernels' csynth Verilog, its leaves, its own module), and its own
    module is the text the system top the XSI gates run holds for that sub-top."""
    from pathlib import Path

    from examples.markov.markov_build import system
    from waveflow.build.system_dag import rtl_rel
    from waveflow.build.system_top import SystemTopSpec

    spec = SystemTopSpec.from_system(system(), top=TOP)
    top_v = spec.render_verilog()
    for sub in spec.subs:
        root = Path(manifest["ips"][sub.name]["root"])
        leaves, kernels = spec.sources_of(sub.name)
        want = {p.name: p.read_bytes() for p in leaves}
        want |= {f.name: f.read_bytes() for m in kernels
                 for f in sorted((HERE / rtl_rel(m)).glob("*.v"))}
        got = {f.name: f.read_bytes() for f in (root / "src").glob("*.v")}
        own = got.pop(f"{sub.module}.v").decode().replace("\r\n", "\n")
        assert got == want, sub.name
        module = own[own.index(f"module {sub.module} ("):]
        assert module in top_v.replace("\r\n", "\n"), sub.name
