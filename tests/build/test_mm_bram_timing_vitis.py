"""Timing closure of the BRAM window's three leaves (``plans/bram_fullrate_port.md``, Stage 3).

An adaptor with one window -- ``axi_slave_front`` and the leaf on port A of a ``bram_t2p``, rendered by
``render_view_slot`` exactly as a system top renders it -- synthesized, placed and routed out of context
on the RFSoC 4x2's ``xczu48dr`` at 4 ns (250 MHz), at 128 and 512 bits, for ``mm_bram_port`` (two cycles
a read word), ``mm_bram_port_p1`` (one, the BRAM's output straight to the read channel) and
``mm_bram_port_pipe`` (one, a registered response).  The window is 16 KB: one channel's block of the
emitter detection example's ring (1,024 words at 128 bits; 256 at 512).

**Inside a ring of registers** (``waveflow.build.rtl_ooc.registered_wrapper``): every AXI pin and every
pin of the memory's port B -- the kernel's, brought out so the memory is the true dual-port a shared
window is -- goes through one flop.  Out of context a path from or to a bare port is not timed, so
without the ring p1's BRAM-to-RDATA path would "close" by never being looked at.  In the ring it is the
path a neighbour that registers its inputs (a crossbar's register slice, a master) sees.

**The gate:** the new leaf closes (setup and hold, WNS >= 0) at both widths; p1 and ``mm_bram_port`` are
measured and reported, not required.  Each run is a few minutes and is cached by the RTL's content
(``impl_registered_ooc``); ``python -m tests.build.test_mm_bram_timing_vitis`` runs the six in
parallel first.  Run: ``pytest tests/build/test_mm_bram_timing_vitis.py -m vitis`` (Vivado 2025.1).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

WORK = Path(__file__).resolve().parent / "_ooc_work" / "bram_timing"
PART = "xczu48dr-ffvg1517-2-e"
PERIOD_NS = 4.0
WINDOW_LAW = 14                     # 16 KB
LEAVES = ("mm_bram_port", "mm_bram_port_p1", "mm_bram_port_pipe")
WIDTHS = (128, 512)
CONFIGS = [(leaf, dw) for dw in WIDTHS for leaf in LEAVES]
_BRAMS = "[get_cells -quiet -hierarchical -filter {PRIMITIVE_GROUP == BLOCKRAM}]"
_RDATA = "[get_cells -quiet {o_s_axi_RDATA_reg*}]"
#: The path groups this gate is about, beside the ring's own (``RING_PATHS``, whose "from BRAM" and
#: "to outputs" also hold the memory's port B -> ``kb_dout`` path, the same in every leaf): into the AXI
#: read data's flops, from the memory straight to them -- p1's path; none exists when the response is
#: registered -- and into the memory (address, enable, data).
READ_PATHS: dict[str, str] = {
    "to_rdata": f"-to {_RDATA}",
    "bram_to_rdata": f"-from {_BRAMS} -to {_RDATA}",
    "to_bram": f"-to {_BRAMS}",
    # the read data's first stretch: from the memory to the first flop on port A's side (any flop but
    # port B's kb_dout ring flops) -- a fabric flop, the BRAM's own output register, the crossbar's
    "bram_read": f"-from {_BRAMS} -to [get_cells -quiet -hierarchical "
                 "-filter {IS_SEQUENTIAL && NAME !~ \"o_kb_dout*\"}]",
}

# --- behind the crossbar (the emitter example's ring sits behind it) -------------------------------

#: Masters on the crossbar: the contention gate's largest count.  MI0 is the window, MI1 a stub (the IP
#: needs a second slot; nothing addresses it).
XBAR_NSI = 4
XBAR_LEAVES = ("mm_bram_port_p1", "mm_bram_port_pipe")
XBAR_CONFIGS = [(leaf, dw) for dw in WIDTHS for leaf in XBAR_LEAVES]
_SI_RDATA = "[get_cells -quiet {o_s*_axi_RDATA_reg*}]"
XBAR_PATHS: dict[str, str] = {
    **READ_PATHS,
    "to_rdata": f"-to {_SI_RDATA}",                           # the masters' read data, at the SI
    "bram_to_rdata": f"-from {_BRAMS} -to {_SI_RDATA}",       # ... straight from the memory
    "bram_to_xbar": f"-from {_BRAMS} -to [get_cells -quiet -hierarchical "
                    "-filter {IS_SEQUENTIAL && NAME =~ \"u_dut/u_xbar/*\"}]",
}


def xbar_cfg(dw: int):
    from waveflow.build.axi_xbar import AxiXbarConfig, AxiXbarRange
    from waveflow.build.system_top import default_id_width

    return AxiXbarConfig(name=f"xbar_bwt_{XBAR_NSI}x2_{dw}", n_si=XBAR_NSI,
                         mi=[AxiXbarRange(0x0000_0000, WINDOW_LAW), AxiXbarRange(0x0001_0000, 12)],
                         data_width=dw, addr_width=32, id_width=default_id_width(XBAR_NSI))


def xbar_dut_name(leaf: str, dw: int) -> str:
    return f"xw_{leaf}_{dw}"


def render_xbar_dut(leaf: str, dw: int) -> str:
    """The window's slot behind the generated ``axi_crossbar``, as ``system_top`` wires a memory on
    the crossbar: the masters' AXI ports ``s<k>_axi``, the crossbar, MI0 the window's front, MI1 a
    stub, the memory's port B ``kb_*``."""
    from waveflow.build.axi_xbar import (
        axi_port_decls,
        axi_signals,
        axi_wire_decls,
        render_xbar_instance,
    )
    from waveflow.build.mm_adaptor_gen import BRAM_VIEWS, mi_wire_signals, render_view_slot

    cfg = xbar_cfg(dw)
    idw = cfg.id_width
    si = [f"s{k}_axi" for k in range(cfg.n_si)]
    mi = ["mi0_axi", "mi1_axi"]
    ports = ["input  wire ap_clk", "input  wire ap_rst_n"]
    for p in si:
        ports += axi_port_decls(p, axi_signals(dw, 32, idw), facing="slave")
    ports += ["input  wire [31:0] kb_addr", "input  wire kb_en", "input  wire [1:0] kb_we",
              f"input  wire [{dw - 1}:0] kb_din", f"output wire [{dw - 1}:0] kb_dout"]
    body = []
    for p in mi:
        body += ["  " + d for d in axi_wire_decls(p, mi_wire_signals(dw, 32, idw))]
    body.append(render_xbar_instance(cfg, "u_xbar", si, mi))
    body += [f"  assign mi1_axi_{name} = 0;" for name, _w, m2s in mi_wire_signals(dw, 32, idw)
             if not m2s]
    view = BRAM_VIEWS[leaf]("win", kport="kb", baw=_baw(dw), law=WINDOW_LAW)
    body.append(render_view_slot(view, "mi0_axi", dw, 32, idw))
    name = xbar_dut_name(leaf, dw)
    return (f"// {name}.v -- GENERATED by tests/build/test_mm_bram_timing_vitis.py (behind the "
            f"crossbar).\n`timescale 1ns/1ps\nmodule {name} (\n  " + ",\n  ".join(ports) + "\n);\n"
            + "\n".join(body) + "endmodule\n")


def _baw(dw: int) -> int:
    return WINDOW_LAW - (dw // 8).bit_length() + 1


def dut_name(leaf: str, dw: int) -> str:
    return f"bw_{leaf}_{dw}"


def render_dut(leaf: str, dw: int) -> str:
    """The adaptor with one window as its own module: the AXI slave port ``s_axi``, the memory's port
    B ``kb_*``, and ``render_view_slot``'s front, leaf and memory between them."""
    from waveflow.build.axi_xbar import axi_port_decls, axi_signals
    from waveflow.build.mm_adaptor_gen import BRAM_VIEWS, render_view_slot

    view = BRAM_VIEWS[leaf]("win", kport="kb", baw=_baw(dw), law=WINDOW_LAW)
    ports = ["input  wire ap_clk", "input  wire ap_rst_n"]
    ports += axi_port_decls("s_axi", axi_signals(dw, 32, 1), facing="slave")
    ports += ["input  wire [31:0] kb_addr", "input  wire kb_en", "input  wire [1:0] kb_we",
              f"input  wire [{dw - 1}:0] kb_din", f"output wire [{dw - 1}:0] kb_dout"]
    name = dut_name(leaf, dw)
    return (f"// {name}.v -- GENERATED by tests/build/test_mm_bram_timing_vitis.py.\n"
            f"`timescale 1ns/1ps\nmodule {name} (\n  " + ",\n  ".join(ports) + "\n);\n"
            + render_view_slot(view, "s_axi", dw, 32, 1) + "endmodule\n")


def run(leaf: str, dw: int, force: bool = False, xbar: bool = False):
    """Synthesize, place and route one configuration (cached): the window's slot alone, or behind
    the crossbar (*xbar*)."""
    from waveflow.build.mm_adaptor_gen import BRAM_VIEWS, leaf_module_sources
    from waveflow.build.rtl_ooc import impl_registered_ooc

    src_dir = WORK / "src"
    src_dir.mkdir(parents=True, exist_ok=True)
    name = xbar_dut_name(leaf, dw) if xbar else dut_name(leaf, dw)
    dut = src_dir / f"{name}.v"
    text = render_xbar_dut(leaf, dw) if xbar else render_dut(leaf, dw)
    if not dut.exists() or dut.read_text(encoding="utf-8") != text:
        dut.write_text(text, encoding="utf-8")
    view = BRAM_VIEWS[leaf]
    sources = [dut, *leaf_module_sources("axi_slave_front"), *leaf_module_sources(view.module),
               *leaf_module_sources(view.mem_module)]
    ips = ()
    if xbar:
        from waveflow.build.axi_xbar import generate_axi_xbar
        ips = (generate_axi_xbar(xbar_cfg(dw), WORK / "ip").root,)
    return impl_registered_ooc(name, sources, part=PART, period_ns=PERIOD_NS, out_dir=WORK,
                               paths=XBAR_PATHS if xbar else READ_PATHS, ips=ips, force=force)


#: Every run: (where, leaf, bits) -- the window's slot alone, or behind the crossbar.
RUNS = [("alone", leaf, dw) for leaf, dw in CONFIGS] + [("xbar", leaf, dw) for leaf, dw in XBAR_CONFIGS]

HEADER = ("| where | leaf | bits | WNS (ns) | WHS (ns) | LUT | FF | BRAM36 | BRAM out. reg A/B | "
          "BRAM -> first flop (ns) | -> RDATA (ns) | BRAM -> RDATA (ns) | BRAM -> crossbar (ns) | "
          "-> BRAM (ns) | worst path |\n" + "|---" * 15 + "|")


def row(where: str, leaf: str, dw: int, r) -> str:
    c, p, b = r.impl, r.paths, r.bram_out_regs

    def ns(k):
        if k not in p:
            return "n/a"
        return "none" if p[k] is None else f"{p[k]:+.3f}"

    return (f"| {where} | `{leaf}` | {dw} | {r.wns_ns:+.3f} | {r.whs_ns:+.3f} | {c.get('lut')} | "
            f"{c.get('ff')} | {c.get('bram', 0) / 2:g} | {b.get('doa_reg')}/{b.get('dob_reg')} of "
            f"{b.get('cells')} | {ns('bram_read')} | {ns('to_rdata')} | {ns('bram_to_rdata')} | "
            f"{ns('bram_to_xbar')} | {ns('to_bram')} | {r.critical_path} |")


@pytest.fixture(scope="module")
def results():
    from waveflow.toolchain.toolchain import find_vivado_path

    if not find_vivado_path():
        pytest.skip("Vitis gate prerequisite missing: Vivado")
    out = {(w, leaf, dw): run(leaf, dw, xbar=w == "xbar") for w, leaf, dw in RUNS}
    print("\n" + HEADER + "\n" + "\n".join(row(*k, out[k]) for k in RUNS))
    return out


@pytest.mark.vitis
@pytest.mark.parametrize("where", ["alone", "xbar"])
@pytest.mark.parametrize("dw", WIDTHS)
def test_the_pipelined_leaf_closes_at_4ns(results, where, dw):
    r = results[(where, "mm_bram_port_pipe", dw)]
    assert r.tool.startswith("vivado"), r.tool
    assert r.met, f"mm_bram_port_pipe ({where}) at {dw} bits misses 4 ns: WNS {r.wns_ns}, " \
                  f"WHS {r.whs_ns}, {r.critical_path}"
    # Registered, by construction: no timing path runs from the memory to the read data's flops --
    # nor, behind the crossbar, into the crossbar.
    assert r.paths["bram_to_rdata"] is None, r.paths
    assert r.paths["to_rdata"] is not None, r.paths
    if where == "xbar":
        assert r.paths["bram_to_xbar"] is None, r.paths


@pytest.mark.vitis
@pytest.mark.parametrize("where,leaf,dw", RUNS)
def test_each_run_is_measured_whole(results, where, leaf, dw):
    """Every configuration ran to a routed design and a readable report, timed at the one clock asked
    for (an IP's own constraints disabled): the memory is block RAM (16 KB is at least four RAMB36),
    and the DUT's own figures exclude the ring's flops."""
    r = results[(where, leaf, dw)]
    assert r.clocks == {"ap_clk": PERIOD_NS}, f"{where} {leaf} {dw}: timed at {r.clocks}"
    assert r.wns_ns is not None and r.whs_ns is not None, r
    assert r.impl.get("bram", 0) >= 8, f"{leaf} {dw}: the memory is not block RAM: {r.impl}"
    assert set(r.paths) >= {"to_outputs", "from_inputs", "from_bram", *READ_PATHS}, r.paths
    assert r.paths["from_bram"] is not None, f"{leaf} {dw}: no path from the BRAM was timed"
    for k in ("to_bram", "to_rdata", "bram_read"):
        assert r.paths[k] is not None, (k, r.paths)
    assert r.bram_out_regs["cells"] >= 4, r.bram_out_regs


if __name__ == "__main__":
    # Run every configuration in parallel (each caches its result), then print the table.
    from concurrent.futures import ProcessPoolExecutor

    force = "--force" in sys.argv
    with ProcessPoolExecutor(max_workers=3) as pool:
        futs = {k: pool.submit(run, k[1], k[2], force, k[0] == "xbar") for k in RUNS}
        done = {k: f.result() for k, f in futs.items()}
    print(HEADER)
    for k in RUNS:
        print(row(*k, done[k]))
