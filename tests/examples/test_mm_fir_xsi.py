"""mm_fir rung 3 (plans/mm_slave_adaptor.md): the synthesized kernel behind the adaptor, under XSI.

Everything is real RTL: AMD's crossbar, the hand-written adaptor leaves (register bank, queue in,
queue out) and the csynth'd ``mm_fir`` kernel, joined by a generated wrapper.  The host is a C++
state machine running the same protocol as the pysim ``FirHost``: commit a config, poll the status
until the config is RECEIVED, wait for room before each packet, drain the outputs between packets, and
switch taps mid-stream.  The output must equal the numpy golden bit for bit, and the status must show
both configs received and none late.

Run: ``pytest tests/examples/test_mm_fir_xsi.py -m xsi`` (needs Vivado, and
``python -m examples.mm_fir.mm_fir_build`` for the kernel's csynth).
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from examples.mm_fir.mm_fir import (
    DW,
    QDEPTH,
    QIN,
    QOUT,
    REGS,
    FirCfg,
    FirStatus,
    MmFirSystem,
    fir_golden,
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
from waveflow.build.trace_steps import rtl_staleness
from waveflow.build.xsi_workspace import XsiWorkspace
from waveflow.toolchain.toolchain import find_vivado_path

REPO = Path(__file__).resolve().parents[2]
ROOT = REPO / "examples" / "mm_fir"
RTL = ROOT / "mm_fir_proj" / "solution1" / "syn" / "verilog"
WORK = REPO / "tests" / "build" / "_xsi_work"

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
    return (f"// {top}.v -- GENERATED by tests/examples/test_mm_fir_xsi.py (mm_fir rung 3).\n"
            f"`timescale 1ns/1ps\nmodule {top} (\n  " + ",\n  ".join(ports) + "\n);\n"
            + "\n".join(body) + "\nendmodule\n")


def _field_pos(schema, name: str) -> tuple[int, int]:
    """(word, bit) where *name* starts in *schema*'s 64-bit serialization -- read off the schema's own
    serializer, so the testbench never restates the layout."""
    words = np.asarray(schema(**{name: 1}).serialize(word_bw=DW), dtype=np.uint64)
    for i, w in enumerate(words):
        if int(w):
            return i, int(w).bit_length() - 1
    raise AssertionError(name)


def _host_actions(x) -> list[tuple]:
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
    for a in _host_actions(x):
        if a[0] == "W":
            rows.append(f"    {{0, 0x{a[1]:x}ull, {{{', '.join(f'0x{w:x}ull' for w in a[2])}}}, 0}},")
        else:
            rows.append(f"    {{{kinds[a[0]]}, 0, {{}}, {a[1]}}},")
    nw, nb = _field_pos(FirStatus, "ncfg")
    sw, sb = _field_pos(FirStatus, "nsamp")
    lw, lb = _field_pos(FirStatus, "late")
    return f'''// mm_fir_tb.cpp -- GENERATED by tests/examples/test_mm_fir_xsi.py: host program -> crossbar ->
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


def _parse_kv(out: str, tag: str) -> dict[str, int]:
    line = next(ln for ln in out.splitlines() if ln.startswith(tag + " "))
    return {k: int(v) for k, v in (kv.split("=") for kv in line.split()[1:])}


@pytest.fixture(scope="module", params=["per_view", "one_front"])
def fir_run(request) -> tuple[str, str]:
    topology = request.param
    if not find_vivado_path():
        pytest.skip("XSI gate prerequisite missing: Vivado (create_ip + xsim)")
    if not RTL.is_dir():
        pytest.skip(f"XSI gate prerequisite missing: no csynth RTL at {RTL} -- run "
                    f"python -m examples.mm_fir.mm_fir_build")
    stale = rtl_staleness(ROOT, "mm_fir")
    if stale is not None:
        pytest.skip(f"XSI gate prerequisite missing: {stale}")
    ip = generate_axi_xbar(XBARS[topology], WORK / "ip")
    ws = XsiWorkspace(WORK / f"mm_fir_{topology}", top="mm_fir_top")
    ws.prepare(rtl_files=ip.sim_files + leaf_sources() + sorted(RTL.glob("*.v")) + ["mm_fir_top.v"],
               include_dirs=ip.include_dirs, tb_name="mm_fir_tb",
               tb_cpp=render_tb(ws.design_dll, scenario_x()),
               extra_files={"mm_fir_top.v": render_top("mm_fir_top", topology)})
    return topology, ws.run(timeout=3600)


@pytest.mark.xsi
def test_mm_fir_rtl_bit_exact(fir_run):
    _topology, fir_run = fir_run
    done = _parse_kv(fir_run, "DONE")
    assert done["done"] == 1, fir_run[-3000:]
    st = _parse_kv(fir_run, "STATUS")
    assert st == {"nsamp": NSAMP, "ncfg": 2, "late": 0}, st
    y_line = next(ln for ln in fir_run.splitlines() if ln.startswith("Y"))
    y = np.array([np.int64(np.uint64(int(h, 16))) for h in y_line.split()[1:]], dtype=np.int64)
    x = scenario_x()
    assert np.array_equal(y, fir_golden(x, PLAN)), "RTL output differs from the numpy golden"


@pytest.mark.xsi
def test_mm_fir_rtl_cycles(fir_run):
    topology, fir_run = fir_run
    done = _parse_kv(fir_run, "DONE")
    sysm = MmFirSystem(x=list(scenario_x()), plan=PLAN, pkt=PKT, one_front=topology == "one_front")
    sysm.run()
    pysim_cycles = sysm.sim.env.now / sysm.clk.period
    print({"topology": topology, "rtl": done, "pysim_cycles": pysim_cycles})
    assert done["cycles"] == EXPECTED_CYCLES[topology], (
        f"{topology}: cycle count moved: {done} (pysim {pysim_cycles})")


#: Recorded 2026-10-02: host program start to the final status read, 200 samples, one tap switch.
#:
#: The kernel is pipelined at II=1 (csynth: latency 10, interval 1).  Its first version was not -- it
#: read a whole 5-word config and wrote a whole 2-word status inside one firing -- and ran at ~1 sample
#: per 10 cycles: 2096 cycles, 221 bus ops, 55 polls, because every drain found only a few outputs.
#: Moving at most one word per stream per firing fixed it: 857 cycles, 68 ops, 2 polls.
#:
#: pysim (crossbar latency_init = 4) predicts 709, 17% optimistic.  Per packet the RTL takes ~57 cycles
#: and pysim ~51, and the difference is the C++ host's own pacing: AxiMmMaster starts each op two
#: cycles after the previous one ends, and the protocol issues four ops per packet.  That is the
#: testbench, not the system; the adaptor alone tracks RTL within 2 cycles (tests/hw/test_mm_queue.py).
#:
#: one_front (Stage 4, recorded the same day): 823 -- 34 fewer over the same 68 ops.  Where the half
#: cycle per op comes from (a 1x2 instead of a 1x3 crossbar, or one front instead of three) has NOT
#: been isolated; both runs are bit-exact and pysim predicts 709 for each.
EXPECTED_CYCLES = {"per_view": 857, "one_front": 823}
