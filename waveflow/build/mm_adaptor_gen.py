"""mm_adaptor_gen.py — Verilog wiring for the memory-mapped slave adaptor (``plans/mm_slave_adaptor.md``).

The adaptor is hand-written leaves (``waveflow/build/rtl/``) joined by generated wiring.  This module
is the wiring: it emits the nets and instances that put :file:`axi_slave_front.v` and the leaves
behind an AXI4 port group, and lists the leaf sources xvlog needs.  It writes no datapath — the
leaves are fixed files whose widths and depths ride on Verilog parameters.

Two shapes:

* :func:`render_view_slot` — one view per front, each view its own crossbar MI slot (Stages 1-2);
* :func:`render_adaptor_slot` — several views behind ONE front, with a generated address decoder
  (Stage 4).  The decoder is the only generated logic, and it is a table.
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


@dataclass(frozen=True)
class RegBankView:
    """A register-bank window (``mm_regbank.v``): ``ncfg`` config words behind shadow-and-commit,
    sent to the kernel on the AXIS group ``cfg_axis``; ``nstat`` status words, taken from the
    kernel's AXIS group ``status_axis``.  Layout: shadow at ``[0, W/2)``, COMMIT at ``W/2``, status
    at ``3W/4`` (``W = 2**law`` bytes)."""

    name: str
    ncfg: int
    nstat: int
    cfg_axis: str
    status_axis: str
    law: int = 12

    def __post_init__(self) -> None:
        if self.law < 12:
            raise ValueError("a register-bank window is at least 4 KB (law >= 12)")
        if not 1 <= self.ncfg or not 1 <= self.nstat:
            raise ValueError("a register bank needs at least one config and one status word")

    module = "mm_regbank"

    def commit_offset(self) -> int:
        return 1 << (self.law - 1)

    def status_offset(self) -> int:
        return 3 << (self.law - 2)


@dataclass(frozen=True)
class BramView:
    """A BRAM window (``mm_bram_port.v`` on port A of a ``bram_t2p`` holding ``2**baw`` words).

    The memory is instantiated with the view; its port B -- the kernel's -- lands on the nets
    ``<kport>_addr`` / ``_en`` / ``_we`` / ``_din`` / ``_dout``.  The leaf's read latency is read from
    ``bram_t2p.v``'s published ``READ_LATENCY`` (:func:`bram_read_latency`), never declared here."""

    name: str
    kport: str
    baw: int = 9
    law: int = 12

    def __post_init__(self) -> None:
        if self.law < 12:
            raise ValueError("a BRAM window is at least 4 KB (law >= 12)")
        if self.baw < 1:
            raise ValueError("a BRAM needs at least one address bit")

    module = "mm_bram_port"


def bram_read_latency() -> int:
    """``bram_t2p.v``'s published read latency -- the single source the leaf's ``LAT`` comes from."""
    from waveflow.build.rtl_gen import RtlModule, rtl_read_latency
    lat = rtl_read_latency(RtlModule(module="bram_t2p", files=("bram_t2p.v",), ports={}, params=(),
                                     clock="clk"))
    if lat is None:                                     # pragma: no cover - guarded by the file
        raise ValueError("bram_t2p.v publishes no READ_LATENCY")
    return lat


def leaf_sources() -> list[Path]:
    """Every hand-written adaptor source, for an xvlog file list (order-independent)."""
    return [RTL_DIR / f for f in ("mm_sync_fifo.v", "axi_slave_front.v", "mm_queue_in.v",
                                  "mm_queue_out.v", "mm_regbank.v", "mm_bram_port.v", "bram_t2p.v")]


# ---------------------------------------------------------------------------
# Pieces: the request-bus nets, the front, a leaf
# ---------------------------------------------------------------------------

_AXIS = ("TDATA", "TVALID", "TREADY", "TLAST")
_ZERO1 = "1'b0"


def _axis_conns(side: str, prefix: str) -> str:
    return ",\n".join(f"    .{side}_{s}({prefix}_{s})" for s in _AXIS)


def _req_conns(n: str) -> list[str]:
    return [
        f"    .req_valid({n}_req_valid), .req_ready({n}_req_ready), .req_we({n}_req_we),",
        f"    .req_addr({n}_req_addr), .req_wdata({n}_req_wdata),",
        f"    .rsp_valid({n}_rsp_valid), .rsp_ready({n}_rsp_ready), .rsp_rdata({n}_rsp_rdata),",
        f"    .rsp_err({n}_rsp_err)",
    ]


def _req_wires(n: str, law: int, dw: int) -> list[str]:
    """The request-bus nets ``<n>_req_*`` / ``<n>_rsp_*`` (the contract in axi_slave_front.v)."""
    return [
        f"  wire {n}_req_valid, {n}_req_ready, {n}_req_we, {n}_rsp_valid, {n}_rsp_ready, {n}_rsp_err;",
        f"  wire [{law - 1}:0] {n}_req_addr;",
        f"  wire [{dw - 1}:0] {n}_req_wdata, {n}_rsp_rdata;",
    ]


def _front_inst(n: str, law: int, axi: str, dw: int, addr_width: int, id_width: int) -> list[str]:
    """``axi_slave_front`` behind the AXI4 nets ``<axi>_<SIG>``, driving the request bus ``<n>_req_*``."""
    lines = [f"  axi_slave_front #(.DW({dw}), .AW({addr_width}), .IDW({id_width}), .LAW({law})) "
             f"u_{n}_front (", "    .ap_clk(ap_clk), .ap_rst_n(ap_rst_n),"]
    lines += [f"    .s_axi_{s}({axi}_{s})," for s in _FRONT_SIGS]
    lines += _req_conns(n)
    lines.append("  );")
    return lines


def _leaf_inst(view, dw: int) -> list[str]:
    """The view's hand-written leaf on the request bus ``<view.name>_req_*``."""
    n = view.name
    if isinstance(view, QueueView):
        side = "m_axis" if view.kind == "in" else "s_axis"
        head = f"  {view.module} #(.DW({dw}), .LAW({view.law}), .DEPTH({view.depth})) u_{n} ("
        # The interrupt (plans/mm_irq.md): the net <view>_irq, for the top to route to its host.
        streams = [_axis_conns(side, view.axis) + ",", f"    .irq({n}_irq)"]
        req = _req_conns(n)
        req[-1] += ","
        return [f"  wire {n}_irq;", head, "    .ap_clk(ap_clk), .ap_rst_n(ap_rst_n),", *req,
                *streams, "  );"]
    elif isinstance(view, RegBankView):
        head = (f"  mm_regbank #(.DW({dw}), .LAW({view.law}), .NCFG({view.ncfg}), "
                f".NSTAT({view.nstat})) u_{n} (")
        streams = [_axis_conns("m_cfg", view.cfg_axis) + ",", _axis_conns("s_status", view.status_axis)]
    elif isinstance(view, BramView):
        k = view.kport
        mem = [
            f"  wire [31:0] {n}_a_addr; wire {n}_a_en; wire [1:0] {n}_a_we;",
            f"  wire [{dw - 1}:0] {n}_a_din, {n}_a_dout;",
            f"  bram_t2p #(.DW({dw}), .AW({view.baw})) u_{n}_mem (",
            "    .clk(ap_clk),",
            f"    .a_addr({n}_a_addr), .a_en({n}_a_en), .a_din({n}_a_din), .a_we({n}_a_we), "
            f".a_dout({n}_a_dout),",
            f"    .b_addr({k}_addr), .b_en({k}_en), .b_din({k}_din), .b_we({k}_we), .b_dout({k}_dout)",
            "  );",
        ]
        head = (f"  mm_bram_port #(.DW({dw}), .LAW({view.law}), .BAW({view.baw}), "
                f".LAT({bram_read_latency()})) u_{n} (")
        streams = [f"    .bram_addr({n}_a_addr), .bram_en({n}_a_en), .bram_we({n}_a_we),",
                   f"    .bram_din({n}_a_din), .bram_dout({n}_a_dout)"]
        req = _req_conns(n)
        req[-1] += ","
        return [*mem, head, "    .ap_clk(ap_clk), .ap_rst_n(ap_rst_n),", *req, *streams, "  );"]
    else:
        raise TypeError(f"no adaptor leaf for view type {type(view).__name__}")
    req = _req_conns(n)
    req[-1] += ","
    return [head, "    .ap_clk(ap_clk), .ap_rst_n(ap_rst_n),", *req, *streams, "  );"]


def _title(view) -> str:
    if isinstance(view, QueueView):
        return f"queue view '{view.name}' ({view.module}, depth {view.depth})"
    if isinstance(view, BramView):
        return f"BRAM view '{view.name}' ({1 << view.baw} words, kernel on port B '{view.kport}')"
    return f"regbank view '{view.name}' (ncfg {view.ncfg}, nstat {view.nstat})"


# ---------------------------------------------------------------------------
# Shape 1: one view per front
# ---------------------------------------------------------------------------

def render_view_slot(view, axi: str, data_width: int, addr_width: int, id_width: int) -> str:
    """One front + one leaf (any view type) behind the AXI4 nets ``<axi>_<SIG>`` (which must already
    exist).  The leaf's stream side lands on nets the enclosing module provides as ports or wires."""
    lines = [f"  // --- {_title(view)} ---"]
    lines += _req_wires(view.name, view.law, data_width)
    lines += _front_inst(view.name, view.law, axi, data_width, addr_width, id_width)
    lines += _leaf_inst(view, data_width)
    return "\n".join(lines) + "\n"


def render_queue_slot(view: QueueView, axi: str, data_width: int, addr_width: int,
                      id_width: int) -> str:
    """:func:`render_view_slot` for a queue view (the Stage 1 name, kept for its callers)."""
    return render_view_slot(view, axi, data_width, addr_width, id_width)


# ---------------------------------------------------------------------------
# Shape 2: several views behind one front (Stage 4)
# ---------------------------------------------------------------------------

#: Every view window is 4 KB in a multi-view adaptor: the view index is the address above bit 12.
VIEW_LAW = 12


def adaptor_law(nviews: int) -> int:
    """Local address bits of an adaptor holding *nviews* 4 KB windows (a power-of-two span)."""
    k = 0
    while (1 << k) < nviews:
        k += 1
    return VIEW_LAW + k


def _mux(views, sig: str, sel: str, default: str) -> str:
    expr = default
    for k in reversed(range(len(views))):
        expr = f"({sel} == {k}) ? {views[k].name}_{sig} : {expr}"
    return expr


def render_adaptor_slot(name: str, views, axi: str, data_width: int, addr_width: int,
                        id_width: int) -> str:
    """Several views behind ONE front: the Stage 4 adaptor (``plans/mm_slave_adaptor.md``).

    View *k* occupies the 4 KB window at local offset ``k * 0x1000``.  The generated decoder:

    * routes a request to the view its address selects (``req_addr[LAW-1:12]``), passing the low 12
      bits as the view-local address;
    * takes the read response from the view that accepted the read, selected by a register latched at
      the request handshake -- correct because the front has at most one read outstanding;
    * accepts an address in the span's unused tail (when the view count is not a power of two) and
      answers a read there SLVERR with data 0, so a stray read cannot hang the bus.

    ORDERING -- the plan's guarantee 1 -- is the front's: it serves one AXI transaction at a time, so a
    write to one view has reached it before a later transaction is even decoded.
    """
    views = list(views)
    if not views:
        raise ValueError("an adaptor needs at least one view")
    names = [v.name for v in views]
    if len(set(names)) != len(names):
        raise ValueError(f"view names must be unique, got {names}")
    for v in views:
        if v.law != VIEW_LAW:
            raise ValueError(f"view '{v.name}': a multi-view adaptor uses 4 KB windows (law 12)")
    dw, n = data_width, name
    law = adaptor_law(len(views))
    sb = law - VIEW_LAW
    sel, rsel, hole, hole_rsp = f"{n}_sel", f"{n}_rsel", f"{n}_hole", f"{n}_hole_rsp"
    zero_dw = "{" + str(dw) + "{1'b0}}"

    lines = [f"  // --- adaptor '{n}': {len(views)} views behind one front (LAW {law}) ---"]
    lines += _req_wires(n, law, dw)
    lines += _front_inst(n, law, axi, dw, addr_width, id_width)
    rng = f"[{sb - 1}:0] " if sb else ""
    lines.append(f"  wire {rng}{sel} = " + (f"{n}_req_addr[{law - 1}:{VIEW_LAW}];" if sb else "1'b0;"))
    lines.append(f"  reg  {rng}{rsel};")
    lines.append(f"  wire {hole} = " + (f"({sel} >= {len(views)});" if (1 << sb) > len(views) else "1'b0;"))
    lines.append(f"  reg  {hole_rsp};")
    for k, v in enumerate(views):
        vn = v.name
        lines.append(f"  // view {k}: {_title(v)} at local 0x{k << VIEW_LAW:x}")
        lines += _req_wires(vn, VIEW_LAW, dw)
        lines += [
            f"  assign {vn}_req_valid = {n}_req_valid && ({sel} == {k});",
            f"  assign {vn}_req_we    = {n}_req_we;",
            f"  assign {vn}_req_addr  = {n}_req_addr[{VIEW_LAW - 1}:0];",
            f"  assign {vn}_req_wdata = {n}_req_wdata;",
            f"  assign {vn}_rsp_ready = {n}_rsp_ready && !{hole_rsp} && ({rsel} == {k});",
        ]
        lines += _leaf_inst(v, dw)
    lines += [
        f"  assign {n}_req_ready = {hole} ? 1'b1 : ({_mux(views, 'req_ready', sel, _ZERO1)});",
        f"  assign {n}_rsp_valid = {hole_rsp} ? 1'b1 : ({_mux(views, 'rsp_valid', rsel, _ZERO1)});",
        f"  assign {n}_rsp_rdata = {hole_rsp} ? {zero_dw} : ({_mux(views, 'rsp_rdata', rsel, zero_dw)});",
        f"  assign {n}_rsp_err   = {hole_rsp} ? 1'b1 : ({_mux(views, 'rsp_err', rsel, _ZERO1)});",
        "  always @(posedge ap_clk) begin",
        f"    if (!ap_rst_n) begin {rsel} <= 0; {hole_rsp} <= 1'b0; end",
        "    else begin",
        f"      if ({n}_req_valid && {n}_req_ready && !{n}_req_we) begin",
        f"        {rsel} <= {sel};",
        f"        {hole_rsp} <= {hole};",
        f"      end else if ({hole_rsp} && {n}_rsp_ready) begin",
        f"        {hole_rsp} <= 1'b0;",
        "      end",
        "    end",
        "  end",
    ]
    return "\n".join(lines) + "\n"


def axis_port_decls(prefix: str, data_width: int, kernel_reads: bool) -> list[str]:
    """Top-level ports for a kernel-side AXIS group.  ``kernel_reads=True``: the queue pushes to the
    kernel (TDATA/TVALID/TLAST are outputs of the adaptor)."""
    o, i = ("output", "input ") if kernel_reads else ("input ", "output")
    return [f"{o} wire [{data_width - 1}:0] {prefix}_TDATA", f"{o} wire {prefix}_TVALID",
            f"{i} wire {prefix}_TREADY", f"{o} wire {prefix}_TLAST"]


def mi_wire_signals(data_width: int, addr_width: int, id_width: int):
    """The AXI4 signal set of a crossbar MI slot (what the slot renderers connect to)."""
    return axi_signals(data_width, addr_width, id_width, region=True)
