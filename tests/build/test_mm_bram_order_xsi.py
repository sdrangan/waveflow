"""Stage 3 of ``plans/mm_slave_adaptor.md`` — the BRAM window, and the ordering guarantee, under XSI.

Two hosts share a 2x2 crossbar.  Host 0 writes a 256-word burst into a BRAM window; host 1, two cycles
later, rings a doorbell: a one-word packet ``[1 | 256]`` into a queue.  A small hand-written "kernel"
waits for the doorbell and then reads the BRAM through port B **highest address first**, so if the
doorbell overtook the data it reads words that have not been written yet.

* ``one_front`` -- the queue and the BRAM window behind ONE front (Stage 4's decoder).  The front
  serves one transaction at a time, so the doorbell waits behind the whole burst: every word the
  kernel reads must be the host's.  **This is the plan's guarantee 1, measured.**
* ``per_view`` -- each view behind its own front, on its own crossbar slot.  Nothing orders the two
  writes, the short doorbell overtakes the long burst, and the kernel MUST read stale words.  This is
  the negative control: without it, the one_front pass would not show the check can fail.

What the pair establishes is the guarantee's SCOPE: ordering across views holds behind one front and
does not hold across fronts.  A design that needs "data, then doorbell" puts both in one adaptor.

Run: ``pytest tests/build/test_mm_bram_order_xsi.py -m xsi`` (needs Vivado).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from waveflow.build.axi_xbar import (
    AxiXbarConfig,
    AxiXbarRange,
    axi_port_decls,
    axi_signals,
    axi_wire_decls,
    generate_axi_xbar,
    render_xbar_instance,
)
from waveflow.build.mm_adaptor_gen import (
    BramView,
    QueueView,
    leaf_sources,
    mi_wire_signals,
    render_adaptor_slot,
    render_view_slot,
)
from waveflow.build.xsi_workspace import XsiWorkspace
from waveflow.toolchain.toolchain import find_vivado_path

WORK = Path(__file__).resolve().parent / "_xsi_work"
NW = 256
BAW = 9

#: Address map, the same in both topologies: the doorbell queue at 0x0000, the BRAM window at 0x1000.
XBARS = {
    # one front: both views behind MI0 (8 KB); MI1 a stub nothing addresses.
    "one_front": AxiXbarConfig(name="xbar_bo_2x2a", n_si=2,
                               mi=[AxiXbarRange(0x0000, 13), AxiXbarRange(0x1_0000, 12)],
                               data_width=64, addr_width=32, id_width=1),
    # per view: the queue on MI0, the window on MI1 -- two fronts.
    "per_view": AxiXbarConfig(name="xbar_bo_2x2b", n_si=2,
                              mi=[AxiXbarRange(0x0000, 12), AxiXbarRange(0x1000, 12)],
                              data_width=64, addr_width=32, id_width=1),
}
VIEWS = [QueueView("bell", "in", axis="k_bell", depth=16), BramView("win", kport="kb", baw=BAW)]

#: The test "kernel": wait for a doorbell word N on k_bell, then read BRAM port B at N-1, N-2, ..., 0
#: and emit each word on k_out.  Two cycles per word (address, then data -- the memory's latency is 1).
READER_V = r"""
`timescale 1ns/1ps
module bram_reader #(parameter DW = 64) (
    input  wire ap_clk, input wire ap_rst_n,
    input  wire [DW-1:0] k_bell_TDATA, input wire k_bell_TVALID, output wire k_bell_TREADY,
    input  wire k_bell_TLAST,
    output reg  [31:0] kb_addr, output reg kb_en, output wire [1:0] kb_we, output wire [DW-1:0] kb_din,
    input  wire [DW-1:0] kb_dout,
    output wire [DW-1:0] k_out_TDATA, output reg k_out_TVALID, input wire k_out_TREADY
);
    localparam IDLE = 2'd0, ADDR = 2'd1, DATA = 2'd2;
    reg [1:0] st;
    reg [31:0] left;
    assign kb_we = 2'b00;
    assign kb_din = {DW{1'b0}};
    assign k_bell_TREADY = (st == IDLE);
    assign k_out_TDATA = kb_dout;
    always @(posedge ap_clk) begin
        if (!ap_rst_n) begin st <= IDLE; kb_en <= 1'b0; kb_addr <= 0; k_out_TVALID <= 1'b0; left <= 0; end
        else case (st)
            IDLE: if (k_bell_TVALID) begin left <= k_bell_TDATA[31:0]; st <= ADDR; end
            ADDR: begin
                kb_en <= 1'b1; kb_addr <= left - 1; st <= DATA;
            end
            DATA: begin
                kb_en <= 1'b0;
                if (!k_out_TVALID) k_out_TVALID <= 1'b1;
                else if (k_out_TREADY) begin
                    k_out_TVALID <= 1'b0;
                    left <= left - 1;
                    st <= (left == 1) ? IDLE : ADDR;
                end
            end
        endcase
    end
endmodule
"""


def render_top(top: str, topology: str) -> str:
    xbar = XBARS[topology]
    dw, aw, idw = xbar.data_width, xbar.addr_width, xbar.id_width
    ports = ["input wire ap_clk", "input wire ap_rst_n"]
    for s in ("s0_axi", "s1_axi"):
        ports += axi_port_decls(s, axi_signals(dw, aw, idw), facing="slave")
    ports += ["output wire [63:0] k_out_TDATA", "output wire k_out_TVALID", "input wire k_out_TREADY"]
    mi = [f"mi{k}_axi" for k in range(len(xbar.mi))]
    body = []
    for p in mi:
        body += ["  " + d for d in axi_wire_decls(p, mi_wire_signals(dw, aw, idw))]
    body += [f"  wire [{dw - 1}:0] k_bell_TDATA;", "  wire k_bell_TVALID, k_bell_TREADY, k_bell_TLAST;",
             "  wire [31:0] kb_addr; wire kb_en; wire [1:0] kb_we;",
             f"  wire [{dw - 1}:0] kb_din, kb_dout;"]
    body.append(render_xbar_instance(xbar, "u_xbar", ["s0_axi", "s1_axi"], mi))
    if topology == "one_front":
        body.append(render_adaptor_slot("ad", VIEWS, mi[0], dw, aw, idw))
        body += [f"  assign {mi[1]}_{name} = 0;" for name, _w, m2s in mi_wire_signals(dw, aw, idw)
                 if not m2s]
    else:
        for view, p in zip(VIEWS, mi):
            body.append(render_view_slot(view, p, dw, aw, idw))
    body.append("""  bram_reader #(.DW(64)) u_reader (
    .ap_clk(ap_clk), .ap_rst_n(ap_rst_n),
    .k_bell_TDATA(k_bell_TDATA), .k_bell_TVALID(k_bell_TVALID), .k_bell_TREADY(k_bell_TREADY),
    .k_bell_TLAST(k_bell_TLAST),
    .kb_addr(kb_addr), .kb_en(kb_en), .kb_we(kb_we), .kb_din(kb_din), .kb_dout(kb_dout),
    .k_out_TDATA(k_out_TDATA), .k_out_TVALID(k_out_TVALID), .k_out_TREADY(k_out_TREADY)
  );""")
    return (f"// {top}.v -- GENERATED by tests/build/test_mm_bram_order_xsi.py ({topology}).\n"
            f"`timescale 1ns/1ps\nmodule {top} (\n  " + ",\n  ".join(ports) + "\n);\n"
            + "\n".join(body) + "\nendmodule\n")


TB = r'''// bram_order_tb.cpp -- data burst on SI0, doorbell on SI1, the reader checks what it finds.
#include "xsi_bfm.h"
using namespace wfbfm;
int main() {
    XsiSim sim("__DLL__", "bram_order.wdb");
    AxiMmMaster h0(sim.dut(), "s0_axi", 8, 0), h1(sim.dut(), "s1_axi", 8, 1);
    AxisSlave out(sim.dut(), "k_out");
    std::vector<uint64_t> data;
    for (int i = 0; i < __NW__; ++i) data.push_back(0xD000 + (uint64_t)i);
    size_t wd = h0.write(0x1000, data, 1);           // the data: one 256-beat burst into the window
    size_t wb = h1.write(0x0000, {1, __NW__}, 3);     // the doorbell, two cycles later, other master
    std::vector<XsiSimObj*> all = {&h0, &h1, &out};
    auto drive = [&] { for (auto* p : all) p->drive(); };
    sim.reset(drive);
    long cyc = 0;
    for (; cyc < 4000 && !(h0.idle() && h1.idle() && out.count() >= __NW__); ++cyc) {
        sim.clock_low();  for (auto* p : all) p->sample();
        sim.clock_high(); for (auto* p : all) p->update(); drive();
    }
    // The reader emits word N-1 first.  A word read before the burst wrote it is 0 (the memory's
    // initial contents), never 0xD000+i.
    int stale = 0;
    for (size_t j = 0; j < out.count(); ++j)
        if (out.words()[j] != data[__NW__ - 1 - j]) ++stale;
    std::printf("ORDER got=%zu stale=%d data_end=%ld bell_end=%ld first_read=%ld\n", out.count(), stale,
                h0.op(wd).t_end, h1.op(wb).t_end, out.cycle_of_word(1));
    sim.close();
    return 0;
}
'''


def _parse(out: str) -> dict[str, int]:
    line = next(ln for ln in out.splitlines() if ln.startswith("ORDER "))
    return {k: int(v) for k, v in (kv.split("=") for kv in line.split()[1:])}


@pytest.fixture(scope="module", params=["one_front", "per_view"])
def order_run(request):
    if not find_vivado_path():
        pytest.skip("XSI gate prerequisite missing: Vivado (create_ip + xsim)")
    topology = request.param
    ip = generate_axi_xbar(XBARS[topology], WORK / "ip")
    ws = XsiWorkspace(WORK / f"bram_order_{topology}", top="bo_top")
    tb = TB.replace("__DLL__", ws.design_dll).replace("__NW__", str(NW))
    ws.prepare(rtl_files=ip.sim_files + leaf_sources() + ["bram_reader.v", "bo_top.v"],
               include_dirs=ip.include_dirs, tb_name="bram_order_tb", tb_cpp=tb,
               extra_files={"bo_top.v": render_top("bo_top", topology), "bram_reader.v": READER_V})
    return topology, _parse(ws.run())


@pytest.mark.xsi
def test_doorbell_ordering(order_run):
    topology, r = order_run
    print(topology, r)
    assert r["got"] == NW, r
    if topology == "one_front":
        # Guarantee 1: the doorbell could not be served before the burst's B, so nothing is stale.
        assert r["stale"] == 0, f"one front: the doorbell overtook the data ({r})"
        assert r["bell_end"] > r["data_end"], r
    else:
        # The negative control: two fronts, no ordering -- the reader MUST find unwritten words.
        assert r["stale"] > 0, f"per view: expected the doorbell to overtake the data ({r})"
        assert r["bell_end"] < r["data_end"], r
    assert r == EXPECTED[topology], f"{topology}: moved: {r}"


#: Recorded 2026-10-02.  one_front: the burst completes at 263, the doorbell -- issued at cycle 3 but
#: held by the front -- at 267, the first read at 269, nothing stale.  per_view: the doorbell completes
#: at 12, the reader starts at 14 while the burst is still landing (until 263), and 63 of the 256 words
#: it reads (highest address first) have not been written yet.
EXPECTED: dict[str, dict[str, int]] = {
    "one_front": {"got": 256, "stale": 0, "data_end": 263, "bell_end": 267, "first_read": 269},
    "per_view": {"got": 256, "stale": 63, "data_end": 263, "bell_end": 12, "first_read": 14},
}
