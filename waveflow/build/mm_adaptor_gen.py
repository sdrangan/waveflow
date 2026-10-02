"""mm_adaptor_gen.py — Verilog wiring for the memory-mapped slave adaptor (``plans/mm_slave_adaptor.md``).

The adaptor is hand-written leaves (``waveflow/build/rtl/``) joined by generated wiring.  This module
is the wiring: it emits the nets and instances that put one :file:`axi_slave_front.v` and one leaf
behind an AXI4 port group, and lists the leaf sources xvlog needs.  It writes no datapath — the
leaves are fixed files whose widths and depths ride on Verilog parameters.

Stage 1 is one view per front (each view gets its own crossbar MI slot); the multi-view decoder that
puts several views behind one front is Stage 4.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from waveflow.build.axi_xbar import axi_signals

RTL_DIR = Path(__file__).resolve().parent / "rtl"

#: The AXI4 signals axi_slave_front has a pin for.  The rest of a full AXI4 group (LOCK, CACHE, PROT,
#: QOS, REGION) carry nothing a FIFO or register bank can honour, so they stay unconnected nets.
_FRONT_SIGS = ("AWID", "AWADDR", "AWLEN", "AWSIZE", "AWBURST", "AWVALID", "AWREADY", "WDATA", "WSTRB",
               "WLAST", "WVALID", "WREADY", "BID", "BRESP", "BVALID", "BREADY", "ARID", "ARADDR",
               "ARLEN", "ARSIZE", "ARBURST", "ARVALID", "ARREADY", "RID", "RDATA", "RRESP", "RLAST",
               "RVALID", "RREADY")


@dataclass(frozen=True)
class QueueView:
    """One queue window.  ``kind`` is ``"in"`` (bus writes push a stream to the kernel) or ``"out"``
    (the kernel's stream is drained by bus reads).  ``axis`` names the kernel-side AXIS port group:
    nets ``<axis>_TDATA`` / ``_TVALID`` / ``_TREADY`` / ``_TLAST``."""

    name: str
    kind: str
    axis: str
    depth: int = 512
    law: int = 12

    def __post_init__(self) -> None:
        if self.kind not in ("in", "out"):
            raise ValueError(f"QueueView kind must be 'in' or 'out', got {self.kind!r}")
        if self.depth < 2 or self.depth & (self.depth - 1):
            raise ValueError(f"QueueView depth must be a power of two >= 2, got {self.depth}")
        if self.law < 12:
            raise ValueError("a queue window is at least 4 KB (law >= 12): one AXI burst may not "
                             "cross a 4 KB boundary, and the window must hold a maximal burst")

    @property
    def module(self) -> str:
        return "mm_queue_in" if self.kind == "in" else "mm_queue_out"


def leaf_sources() -> list[Path]:
    """Every hand-written adaptor source, for an xvlog file list (order-independent)."""
    return [RTL_DIR / f for f in ("mm_sync_fifo.v", "axi_slave_front.v", "mm_queue_in.v",
                                  "mm_queue_out.v")]


def render_queue_slot(view: QueueView, axi: str, data_width: int, addr_width: int,
                      id_width: int) -> str:
    """One front + one queue leaf behind the AXI4 nets ``<axi>_<SIG>`` (which must already exist).

    The request bus between them is local nets ``<name>_req_*`` / ``<name>_rsp_*``; the leaf's
    stream side lands on ``<axis>_T*`` nets, which the enclosing module provides as ports or wires.
    """
    n, dw = view.name, data_width
    lines = [
        f"  // --- queue view '{n}' ({view.module}, depth {view.depth}) ---",
        f"  wire {n}_req_valid, {n}_req_ready, {n}_req_we, {n}_rsp_valid, {n}_rsp_ready, {n}_rsp_err;",
        f"  wire [{view.law - 1}:0] {n}_req_addr;",
        f"  wire [{dw - 1}:0] {n}_req_wdata, {n}_rsp_rdata;",
        f"  axi_slave_front #(.DW({dw}), .AW({addr_width}), .IDW({id_width}), .LAW({view.law})) "
        f"u_{n}_front (",
        "    .ap_clk(ap_clk), .ap_rst_n(ap_rst_n),",
    ]
    lines += [f"    .s_axi_{s}({axi}_{s})," for s in _FRONT_SIGS]
    lines += [
        f"    .req_valid({n}_req_valid), .req_ready({n}_req_ready), .req_we({n}_req_we),",
        f"    .req_addr({n}_req_addr), .req_wdata({n}_req_wdata),",
        f"    .rsp_valid({n}_rsp_valid), .rsp_ready({n}_rsp_ready), .rsp_rdata({n}_rsp_rdata),",
        f"    .rsp_err({n}_rsp_err)",
        "  );",
        f"  {view.module} #(.DW({dw}), .LAW({view.law}), .DEPTH({view.depth})) u_{n} (",
        "    .ap_clk(ap_clk), .ap_rst_n(ap_rst_n),",
        f"    .req_valid({n}_req_valid), .req_ready({n}_req_ready), .req_we({n}_req_we),",
        f"    .req_addr({n}_req_addr), .req_wdata({n}_req_wdata),",
        f"    .rsp_valid({n}_rsp_valid), .rsp_ready({n}_rsp_ready), .rsp_rdata({n}_rsp_rdata),",
        f"    .rsp_err({n}_rsp_err),",
    ]
    side = "m_axis" if view.kind == "in" else "s_axis"
    lines.append(",\n".join(f"    .{side}_{s}({view.axis}_{s})"
                            for s in ("TDATA", "TVALID", "TREADY", "TLAST")))
    lines.append("  );")
    return "\n".join(lines) + "\n"


def axis_port_decls(prefix: str, data_width: int, kernel_reads: bool) -> list[str]:
    """Top-level ports for a kernel-side AXIS group.  ``kernel_reads=True``: the queue pushes to the
    kernel (TDATA/TVALID/TLAST are outputs of the adaptor)."""
    o, i = ("output", "input ") if kernel_reads else ("input ", "output")
    return [f"{o} wire [{data_width - 1}:0] {prefix}_TDATA", f"{o} wire {prefix}_TVALID",
            f"{i} wire {prefix}_TREADY", f"{o} wire {prefix}_TLAST"]


def mi_wire_signals(data_width: int, addr_width: int, id_width: int):
    """The AXI4 signal set of a crossbar MI slot (what :func:`render_queue_slot` connects to)."""
    return axi_signals(data_width, addr_width, id_width, region=True)
