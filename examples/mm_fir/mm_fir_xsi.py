"""mm_fir_xsi.py — the mm_fir system at RTL: crossbar + adaptor + kernel, driven by a C++ host.

Rung 3 of ``plans/mm_slave_adaptor.md``'s witness.  Everything here is real RTL -- AMD's crossbar
(:mod:`waveflow.build.axi_xbar`), the hand-written adaptor leaves joined by generated wiring
(:mod:`waveflow.build.mm_adaptor_gen`), and the csynth'd ``mm_fir`` kernel -- under one generated
Verilog top, simulated through XSI.  The host is a C++ state machine (:func:`render_tb`) running the
same protocol as the pysim :class:`~examples.mm_fir.mm_fir.FirHost`, its decisions laid out by
:func:`host_actions`.

Two topologies with one address map (view *k* at ``REGS + k * 4 KB``):

* ``per_view``  -- a 1x3 crossbar; each view its own MI slot and its own front;
* ``one_front`` -- all three views behind ONE front and a generated decoder, on MI0 of a 1x2 crossbar.

    out = run_xsi("one_front", work_dir)      # needs Vivado and the kernel's csynth (mm_fir_build)
    parse_kv(out, "STATUS")                   # {'nsamp': 200, 'ncfg': 2, 'late': 0}

The gate is ``tests/examples/test_mm_fir_xsi.py``.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from examples.mm_fir.mm_fir import (
    DW,
    QDEPTH,
    QIN,
    QOUT,
    REGS,
    FirCfg,
    FirStatus,
    make_cfg,
)
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
    QueueView,
    RegBankView,
    leaf_sources,
    mi_wire_signals,
    adaptor_law,
    render_adaptor_slot,
    render_view_slot,
)
from waveflow.build.xsi_workspace import XsiWorkspace

ROOT = Path(__file__).resolve().parent
RTL = ROOT / "mm_fir_proj" / "solution1" / "syn" / "verilog"

#: Two topologies, one address map (view k at REGS + k * 4 KB either way):
#:   per_view  -- a 1x3 crossbar, each view its own MI slot and its own front (Stages 1-2);
#:   one_front -- all three views behind ONE front and a generated decoder (Stage 4), on MI0 of a 1x2
#:                crossbar.  MI1 is a stub nothing addresses: a 1x1 crossbar is degenerate (create_ip
#:                generates an inconsistent 2-MI IP for it -- see AxiXbarConfig), and a real system has
#:                more than one slave anyway.
XBARS = {
    "per_view": AxiXbarConfig(
        name="xbar_mm3_1x3", n_si=1,
        mi=[AxiXbarRange(REGS, 12), AxiXbarRange(QIN, 12), AxiXbarRange(QOUT, 12)],
        data_width=DW, addr_width=32, id_width=1),
    "one_front": AxiXbarConfig(
        name="xbar_mm1_1x2", n_si=1,
        mi=[AxiXbarRange(REGS, adaptor_law(3)), AxiXbarRange(0x0001_0000, 12)],
        data_width=DW, addr_width=32, id_width=1),
}
NCFG = FirCfg.nwords_per_inst(DW)
NSTAT = FirStatus.nwords_per_inst(DW)
VIEWS = [RegBankView("regs", ncfg=NCFG, nstat=NSTAT, cfg_axis="k_cfg", status_axis="k_stat"),
         QueueView("qin", "in", axis="k_in", depth=QDEPTH),
         QueueView("qout", "out", axis="k_out", depth=QDEPTH)]

NSAMP, SWITCH_AT, PKT, POLL = 200, 101, 16, 8
TAPS_A = [3, -1, 4, 1, -5]
TAPS_B = [2, 7, 1, -8, 2, 8, 1, -8]
PLAN = [(0, TAPS_A), (SWITCH_AT, TAPS_B)]


def scenario_x() -> np.ndarray:
    return np.random.default_rng(7).integers(-2000, 2000, size=NSAMP)


def render_top(top: str, topology: str) -> str:
    xbar = XBARS[topology]
    dw, aw, idw = xbar.data_width, xbar.addr_width, xbar.id_width
    ports = ["input wire ap_clk", "input wire ap_rst_n"]
    ports += axi_port_decls("s0_axi", axi_signals(dw, aw, idw), facing="slave")
    mi = [f"mi{k}_axi" for k in range(len(xbar.mi))]
    body = []
    for p in mi:
        body += ["  " + d for d in axi_wire_decls(p, mi_wire_signals(dw, aw, idw))]
    for g in ("k_cfg", "k_stat", "k_in", "k_out"):
        body += [f"  wire [{dw - 1}:0] {g}_TDATA;", f"  wire {g}_TVALID, {g}_TREADY, {g}_TLAST;"]
    # The kernel's ports carry no TLAST (it filters sample by sample and reads a fixed-size config),
    # so the TLASTs the leaves drive go nowhere, and the ones they read are tied low: the status bank
    # completes a message on its NSTAT-th word, and queue out ignores TLAST.
    body += ["  assign k_stat_TLAST = 1'b0;", "  assign k_out_TLAST = 1'b0;"]
    body.append(render_xbar_instance(xbar, "u_xbar", ["s0_axi"], mi))
    if topology == "one_front":
        body.append(render_adaptor_slot("fir_mm", VIEWS, mi[0], dw, aw, idw))
        # The stub on MI1: every slave-driven signal held low.  Never addressed in this test.
        body += [f"  assign {mi[1]}_{name} = 0;" for name, _w, m2s in mi_wire_signals(dw, aw, idw)
                 if not m2s]
    else:
        for view, p in zip(VIEWS, mi):
            body.append(render_view_slot(view, p, dw, aw, idw))
    body.append("""  mm_fir u_fir (
    .ap_clk(ap_clk), .ap_rst_n(ap_rst_n),
    .s_cfg_TDATA(k_cfg_TDATA), .s_cfg_TVALID(k_cfg_TVALID), .s_cfg_TREADY(k_cfg_TREADY),
    .s_in_TDATA(k_in_TDATA), .s_in_TVALID(k_in_TVALID), .s_in_TREADY(k_in_TREADY),
    .m_out_TDATA(k_out_TDATA), .m_out_TVALID(k_out_TVALID), .m_out_TREADY(k_out_TREADY),
    .m_status_TDATA(k_stat_TDATA), .m_status_TVALID(k_stat_TVALID), .m_status_TREADY(k_stat_TREADY)
  );""")
    return (f"// {top}.v -- GENERATED by examples/mm_fir/mm_fir_xsi.py (mm_fir, {topology}).\n"
            f"`timescale 1ns/1ps\nmodule {top} (\n  " + ",\n  ".join(ports) + "\n);\n"
            + "\n".join(body) + "\nendmodule\n")


def field_pos(schema, name: str) -> tuple[int, int]:
    """(word, bit) where *name* starts in *schema*'s 64-bit serialization -- read off the schema's own
    serializer, so the testbench never restates the layout."""
    words = np.asarray(schema(**{name: 1}).serialize(word_bw=DW), dtype=np.uint64)
    for i, w in enumerate(words):
        if int(w):
            return i, int(w).bit_length() - 1
    raise AssertionError(name)


def host_actions(x) -> list[tuple]:
    """The FirHost protocol as a flat action list (same decisions as the pysim host)."""
    acts: list[tuple] = []
    cfgs = sorted(PLAN, key=lambda c: c[0])
    nxt, n = 0, 0
    while n < len(x):
        while nxt < len(cfgs) and cfgs[nxt][0] <= n:
            words = [int(w) for w in make_cfg(cfgs[nxt][1], cfgs[nxt][0]).serialize(word_bw=DW)]
            acts += [("W", REGS, words), ("W", REGS + 0x800, [1]), ("POLL_NCFG", nxt + 1)]
            nxt += 1
        end = min(n + PKT, len(x), cfgs[nxt][0] if nxt < len(cfgs) else len(x))
        chunk = [int(v) & 0xFFFF for v in x[n:end]]
        acts += [("DRAIN", 0), ("WAIT_VAC", len(chunk)), ("W", QIN, [len(chunk)] + chunk)]
        n = end
    acts += [("DRAIN_ALL", len(x)), ("STATUS", 0)]
    return acts


def render_tb(dll: str, x) -> str:
    kinds = {"W": 0, "POLL_NCFG": 1, "DRAIN": 2, "WAIT_VAC": 3, "DRAIN_ALL": 4, "STATUS": 5}
    rows = []
    for a in host_actions(x):
        if a[0] == "W":
            rows.append(f"    {{0, 0x{a[1]:x}ull, {{{', '.join(f'0x{w:x}ull' for w in a[2])}}}, 0}},")
        else:
            rows.append(f"    {{{kinds[a[0]]}, 0, {{}}, {a[1]}}},")
    nw, nb = field_pos(FirStatus, "ncfg")
    sw, sb = field_pos(FirStatus, "nsamp")
    lw, lb = field_pos(FirStatus, "late")
    return f'''// mm_fir_tb.cpp -- GENERATED by examples/mm_fir/mm_fir_xsi.py: host program -> crossbar ->
// adaptor -> mm_fir kernel.  The host is a state machine over AxiMmMaster ops.
#include "xsi_bfm.h"
using namespace wfbfm;

struct Act {{ int kind; uint64_t addr; std::vector<uint64_t> words; uint64_t arg; }};
enum {{ W = 0, POLL_NCFG = 1, DRAIN = 2, WAIT_VAC = 3, DRAIN_ALL = 4, STATUS = 5 }};
static const uint64_t REGS = 0x{REGS:x}, QIN = 0x{QIN:x}, QOUT = 0x{QOUT:x};
static const uint64_t STATUS_A = REGS + 0xC00, OCC_A = QOUT + 0x800;
static const long POLL = {POLL};

static uint32_t field(const std::vector<uint64_t>& w, int word, int bit) {{
    return (uint32_t)(w[word] >> bit);
}}

class HostProgram : public XsiSimObj {{
public:
    HostProgram(AxiMmMaster& m, std::vector<Act> acts) : m_(m), acts_(std::move(acts)) {{}}
    std::vector<uint64_t> y;
    std::vector<uint64_t> final_status;
    bool done() const {{ return ai_ >= acts_.size() && !busy_; }}
    long polls = 0;

    void update() override {{
        ++cyc_;
        if (busy_) {{
            if (!m_.op(op_).done()) return;
            busy_ = false;
            on_done(m_.op(op_));
        }}
        if (!busy_ && ai_ < acts_.size()) issue();
    }}

private:
    void read(uint64_t a, uint32_t n, long delay) {{ op_ = m_.read(a, n, cyc_ + delay); busy_ = true; }}
    void issue() {{
        const Act& a = acts_[ai_];
        const long d = again_ ? POLL : 0;
        switch (a.kind) {{
        case W:         op_ = m_.write(a.addr, a.words); busy_ = true; break;
        case POLL_NCFG: case STATUS: read(STATUS_A, {NSTAT}, d); if (again_) ++polls; break;
        case WAIT_VAC:  read(QIN, 1, d); if (again_) ++polls; break;
        case DRAIN: case DRAIN_ALL:
            if (left_ > 0) read(QOUT, (uint32_t)std::min<long>(left_, 256), 0);
            else           read(OCC_A, 1, d);
            break;
        }}
    }}
    void next() {{ ++ai_; again_ = false; left_ = 0; }}
    void on_done(const AxiMmMaster::Op& o) {{
        const Act& a = acts_[ai_];
        switch (a.kind) {{
        case W: next(); break;
        case POLL_NCFG:
            if (field(o.rdata, {nw}, {nb}) >= a.arg) next(); else again_ = true;
            break;
        case WAIT_VAC: if (o.rdata[0] >= a.arg) next(); else again_ = true; break;
        case STATUS: final_status = o.rdata; next(); break;
        case DRAIN: case DRAIN_ALL:
            if (o.addr == OCC_A) {{
                left_ = (long)o.rdata[0];
                if (left_ == 0) {{
                    if (a.kind == DRAIN || y.size() >= a.arg) next(); else again_ = true;
                }} else {{
                    again_ = false;
                }}
            }} else {{
                y.insert(y.end(), o.rdata.begin(), o.rdata.end());
                left_ -= (long)o.rdata.size();
                if (left_ == 0) {{
                    if (a.kind == DRAIN || y.size() >= a.arg) next(); else again_ = false;
                }}
            }}
            break;
        }}
    }}
    AxiMmMaster& m_;
    std::vector<Act> acts_;
    size_t ai_ = 0, op_ = 0;
    bool busy_ = false, again_ = false;
    long left_ = 0, cyc_ = 0;
}};

int main() {{
    XsiSim sim("{dll}", "mm_fir.wdb");
    AxiMmMaster host(sim.dut(), "s0_axi", 8, 0);
    HostProgram prog(host, {{
{chr(10).join(rows)}
    }});
    std::vector<XsiSimObj*> all = {{&host, &prog}};
    auto drive = [&] {{ for (auto* p : all) p->drive(); }};
    sim.reset(drive);
    long cyc = 0;
    for (; cyc < 200000 && !prog.done(); ++cyc) {{
        sim.clock_low();  for (auto* p : all) p->sample();
        sim.clock_high(); for (auto* p : all) p->update(); drive();
    }}
    std::printf("DONE done=%d cycles=%ld polls=%ld nops=%zu\\n", (int)prog.done(), cyc, prog.polls, host.nops());
    std::printf("STATUS nsamp=%u ncfg=%u late=%u\\n", field(prog.final_status, {sw}, {sb}),
                field(prog.final_status, {nw}, {nb}), field(prog.final_status, {lw}, {lb}));
    for (size_t i = 0; i < host.nops(); ++i) {{
        const AxiMmMaster::Op& o = host.op(i);
        std::printf("OP %c 0x%llx n=%zu s=%ld e=%ld\\n", o.write ? 'W' : 'R', (unsigned long long)o.addr,
                    o.write ? o.wdata.size() : (size_t)o.nwords, o.t_start, o.t_end);
    }}
    std::printf("Y");
    for (uint64_t v : prog.y) std::printf(" %llx", (unsigned long long)v);
    std::printf("\\n");
    sim.close();
    return prog.done() ? 0 : 1;
}}
'''


def parse_kv(out: str, tag: str) -> dict[str, int]:
    line = next(ln for ln in out.splitlines() if ln.startswith(tag + " "))
    return {k: int(v) for k, v in (kv.split("=") for kv in line.split()[1:])}


def output_words(out: str) -> np.ndarray:
    """The outputs the host drained (the ``Y`` line), as signed int64."""
    y_line = next(ln for ln in out.splitlines() if ln.startswith("Y"))
    return np.array([np.int64(np.uint64(int(h, 16))) for h in y_line.split()[1:]], dtype=np.int64)


def run_xsi(topology: str, work_dir, timeout: int = 3600) -> str:
    """Generate the crossbar, render the top and the host program, and run XSI.  Returns the output.

    Needs Vivado (``create_ip`` + xsim) and the kernel's RTL (``python -m examples.mm_fir.mm_fir_build``).
    """
    if topology not in XBARS:
        raise ValueError(f"topology must be one of {sorted(XBARS)}, got {topology!r}")
    if not RTL.is_dir():
        raise FileNotFoundError(f"no csynth RTL at {RTL}: run python -m examples.mm_fir.mm_fir_build")
    work_dir = Path(work_dir)
    ip = generate_axi_xbar(XBARS[topology], work_dir / "ip")
    ws = XsiWorkspace(work_dir / f"mm_fir_{topology}", top="mm_fir_top")
    ws.prepare(rtl_files=ip.sim_files + leaf_sources() + sorted(RTL.glob("*.v")) + ["mm_fir_top.v"],
               include_dirs=ip.include_dirs, tb_name="mm_fir_tb",
               tb_cpp=render_tb(ws.design_dll, scenario_x()),
               extra_files={"mm_fir_top.v": render_top("mm_fir_top", topology)})
    return ws.run(timeout=timeout)
