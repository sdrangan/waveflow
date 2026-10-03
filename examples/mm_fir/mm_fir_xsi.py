"""mm_fir_xsi.py — the mm_fir system at RTL: crossbar + adaptor + kernel, driven by a C++ host.

Rung 3 of ``plans/mm_slave_adaptor.md``'s witness.  Everything here is real RTL -- AMD's crossbar
(:mod:`waveflow.build.axi_xbar`), the hand-written adaptor leaves joined by generated wiring
(:mod:`waveflow.build.mm_adaptor_gen`), and the csynth'd ``mm_fir`` kernel -- under one generated
Verilog top, simulated through XSI.  The host (:func:`render_tb`) is the pysim
:class:`~examples.mm_fir.mm_fir.FirHost` written against the C++ endpoints of
``waveflow/build/xsi/xsi_mm_host.h``: the same writer and reader, the same
:func:`~examples.mm_fir.mm_fir.host_schedule`, and an address map generated from the same pysim
system (:func:`map_header`) -- the testbench names no address.

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
    MM_BASE,
    MM_LAYOUT,
    QDEPTH,
    QIN,
    QOUT,
    QRESP,
    RDEPTH,
    REGS,
    S16,
    FirCfg,
    FirCmdHdr,
    FirRespHdr,
    FirStatus,
    HOST_MAX_OUTSTANDING,
    host_schedule,
    make_cfg,
)
from waveflow.hw.arrayutils import array
from waveflow.hw.mm_host import bases_to_cpp_header
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
#:   per_view  -- a 1x4 crossbar, each view its own MI slot and its own front (Stages 1-2);
#:   one_front -- all four views behind ONE front and a generated decoder (Stage 4), on MI0 of a 1x2
#:                crossbar.  MI1 is a stub nothing addresses: a 1x1 crossbar is degenerate (create_ip
#:                generates an inconsistent 2-MI IP for it -- see AxiXbarConfig), and a real system has
#:                more than one slave anyway.
XBARS = {
    "per_view": AxiXbarConfig(
        name="xbar_mm4_1x4", n_si=1,
        mi=[AxiXbarRange(REGS, 12), AxiXbarRange(QIN, 12), AxiXbarRange(QOUT, 12),
            AxiXbarRange(QRESP, 12)],
        data_width=DW, addr_width=32, id_width=1),
    "one_front": AxiXbarConfig(
        name="xbar_mm1_1x2", n_si=1,
        mi=[AxiXbarRange(REGS, adaptor_law(4)), AxiXbarRange(0x0001_0000, 12)],
        data_width=DW, addr_width=32, id_width=1),
}
NCFG = FirCfg.nwords_per_inst(DW)
NSTAT = FirStatus.nwords_per_inst(DW)
VIEWS = [RegBankView("regs", ncfg=NCFG, nstat=NSTAT, cfg_axis="k_cfg", status_axis="k_stat"),
         QueueView("qin", "in", axis="k_in", depth=QDEPTH),
         QueueView("qout", "out", axis="k_out", depth=QDEPTH),
         QueueView("qresp", "out", axis="k_resp", depth=RDEPTH)]

NSAMP, SWITCH_AT, PKT, POLL = 200, 101, 16, 8

#: The C++ host's bus master keeps one read AND one write outstanding at once (AxiMmMaster's
#: ``overlap_rw``) -- derived from ``mm_fir.HOST_MAX_OUTSTANDING``, the same setting pysim's
#: ``MMIFMaster.max_outstanding`` uses, so the two backends cannot model different masters.
#: AxiMmMaster supports exactly one per direction (overlap) or one in total (not), so any other limit
#: is refused rather than silently approximated.
if HOST_MAX_OUTSTANDING != 1:
    raise ValueError(f"AxiMmMaster models one transaction per direction; HOST_MAX_OUTSTANDING is "
                     f"{HOST_MAX_OUTSTANDING}")
OVERLAP_RW = True
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
    # The queue views' interrupts, for the host (plans/mm_irq.md): the testbench samples these pins.
    ports += [f"output wire irq_{v.name}" for v in VIEWS if isinstance(v, QueueView)]
    mi = [f"mi{k}_axi" for k in range(len(xbar.mi))]
    body = []
    for p in mi:
        body += ["  " + d for d in axi_wire_decls(p, mi_wire_signals(dw, aw, idw))]
    for g in ("k_cfg", "k_stat", "k_in", "k_out", "k_resp"):
        body += [f"  wire [{dw - 1}:0] {g}_TDATA;", f"  wire {g}_TVALID, {g}_TREADY, {g}_TLAST;"]
    # The kernel's ports carry no TLAST (it filters sample by sample and reads a fixed-size config),
    # so the TLASTs the leaves drive go nowhere, and the ones they read are tied low: the status bank
    # completes a message on its NSTAT-th word, and a queue out ignores TLAST.
    body += ["  assign k_stat_TLAST = 1'b0;", "  assign k_out_TLAST = 1'b0;",
             "  assign k_resp_TLAST = 1'b0;"]
    body.append(render_xbar_instance(xbar, "u_xbar", ["s0_axi"], mi))
    if topology == "one_front":
        body.append(render_adaptor_slot("fir_mm", VIEWS, mi[0], dw, aw, idw))
        # The stub on MI1: every slave-driven signal held low.  Never addressed in this test.
        body += [f"  assign {mi[1]}_{name} = 0;" for name, _w, m2s in mi_wire_signals(dw, aw, idw)
                 if not m2s]
    else:
        for view, p in zip(VIEWS, mi):
            body.append(render_view_slot(view, p, dw, aw, idw))
    body += [f"  assign irq_{v.name} = {v.name}_irq;" for v in VIEWS if isinstance(v, QueueView)]
    body.append("""  mm_fir u_fir (
    .ap_clk(ap_clk), .ap_rst_n(ap_rst_n),
    .s_cfg_TDATA(k_cfg_TDATA), .s_cfg_TVALID(k_cfg_TVALID), .s_cfg_TREADY(k_cfg_TREADY),
    .s_in_TDATA(k_in_TDATA), .s_in_TVALID(k_in_TVALID), .s_in_TREADY(k_in_TREADY),
    .m_out_TDATA(k_out_TDATA), .m_out_TVALID(k_out_TVALID), .m_out_TREADY(k_out_TREADY),
    .m_resp_TDATA(k_resp_TDATA), .m_resp_TVALID(k_resp_TVALID), .m_resp_TREADY(k_resp_TREADY),
    .m_status_TDATA(k_stat_TDATA), .m_status_TVALID(k_stat_TVALID), .m_status_TREADY(k_stat_TREADY)
  );""")
    return (f"// {top}.v -- GENERATED by examples/mm_fir/mm_fir_xsi.py (mm_fir, {topology}).\n"
            f"`timescale 1ns/1ps\nmodule {top} (\n  " + ",\n  ".join(ports) + "\n);\n"
            + "\n".join(body) + "\nendmodule\n")


def field_pos(schema, name: str) -> tuple[int, int, int]:
    """(word, bit, width) of *name* in *schema*'s 64-bit serialization -- the position read off the
    schema's own serializer, the width off the field's type, so the testbench never restates the
    layout."""
    words = np.asarray(schema(**{name: 1}).serialize(word_bw=DW), dtype=np.uint64)
    width = int(schema.elements[name]["schema"].bitwidth)
    for i, w in enumerate(words):
        if int(w):
            return i, int(w).bit_length() - 1, width
    raise AssertionError(name)


def address_headers() -> dict[str, str]:
    """The two halves of the address map the C++ host uses (``plans/bus_address_map.md``): the FIR
    TYPE's layout (offsets within the slave, from ``MmFir.mm_views``) and this SYSTEM's bases (where
    the FIR instance is placed).  The testbench combines them -- ``at(mm_fir_layout::qin, FIR)`` -- and
    restates neither.  The same for both topologies: one front or one slot per view, the views sit at
    the same offsets."""
    src = "examples/mm_fir/mm_fir_xsi.py"
    return {"mm_fir_layout.h": MM_LAYOUT.to_cpp_header("mm_fir_layout", source=src),
            "mm_fir_bases.h": bases_to_cpp_header("mm_fir_bases", {"fir": (MM_BASE, MM_LAYOUT.span)},
                                                  source=src)}


def render_tb(dll: str, x) -> str:
    """The C++ host: the pysim :class:`~examples.mm_fir.mm_fir.FirHost`, written against the C++
    endpoints of ``xsi_mm_host.h``.  Same two programs (a writer and a reader on one bus master), the
    same :func:`~examples.mm_fir.mm_fir.host_schedule`, the same polling rules, the same check of
    every response."""
    def hexes(words):
        return ", ".join(f"0x{int(w):x}ull" for w in words)

    rows, tx = [], 0
    for item in host_schedule(len(x), PLAN, PKT):
        if item[0] == "cfg":
            rows.append(f"    {{CFG, {{{hexes(make_cfg(item[1]).serialize(word_bw=DW))}}}, {{}}, 0u, 0u, 0u}},")
        else:
            _, n0, n1, tag, want = item
            hdr = FirCmdHdr(nsamp=n1 - n0, tx_id=tx, cfg_seq=tag).serialize(word_bw=DW)
            samples = array(S16, np.asarray(x[n0:n1], dtype=np.int64)).serialize(word_bw=DW)
            rows.append(f"    {{PKT, {{{hexes(hdr)}}}, {{{hexes(samples)}}}, {n1 - n0}u, {tx}u, {want}u}},")
            tx += 1
    f_nsamp = field_pos(FirStatus, "nsamp")
    f_ncfg = field_pos(FirStatus, "ncfg")
    f_tx = field_pos(FirRespHdr, "tx_id")
    f_seq = field_pos(FirRespHdr, "cfg_seq")

    def fld(words: str, pos) -> str:
        return f"field({words}, {pos[0]}, {pos[1]}, {pos[2]})"

    return f'''// mm_fir_tb.cpp -- GENERATED by examples/mm_fir/mm_fir_xsi.py: host program -> crossbar ->
// adaptor -> mm_fir kernel.  The host is the pysim FirHost on the C++ endpoints of xsi_mm_host.h:
// a writer and a reader sharing one AxiMmMaster, and no address anywhere in this file.
#include "xsi_bfm.h"
#include "xsi_mm_host.h"
#include "mm_fir_layout.h"
#include "mm_fir_bases.h"
using namespace wfbfm;

enum {{ CFG = 0, PKT = 1 }};
/// One schedule entry: a config (words = the FirCfg), or a packet (words = its FirCmdHdr, samples =
/// its samples as the serializer packs them -- four int16 to a word -- nsamp = how many, tx / want = the
/// response the host expects back).
struct Item {{ int kind; std::vector<uint64_t> words, samples; uint32_t nsamp, tx, want; }};
static const std::vector<Item> SCHEDULE = {{
{chr(10).join(rows)}
}};
static const long POLL = {POLL};
/// Where this system placed the FIR -- its views are this plus the type's layout offsets.
static const uint64_t FIR = mm_fir_bases::FIR_BASE;
static const uint32_t NSAMP = {len(x)};

static uint32_t field(const std::vector<uint64_t>& w, int word, int bit, int width) {{
    const uint64_t v = w[word] >> bit;
    return (uint32_t)(width >= 64 ? v : (v & ((1ull << width) - 1)));
}}

/// Commits each config; sends each packet as two queue-in packets -- its header, then its samples.
/// It never waits for a config to be received: the header's cfg_seq makes the kernel wait.  It waits
/// for room in queue in on queue in's interrupt -- no polling.
class Writer : public XsiSimObj {{
public:
    Writer(AxiMmMaster& m, const IrqPin& qin_irq)
        : cfg_(m, at(mm_fir_layout::regs, FIR), POLL), qin_(m, at(mm_fir_layout::qin, FIR), POLL) {{
        qin_.use_irq(qin_irq);
    }}
    bool done() const {{ return i_ >= SCHEDULE.size() && phase_ == IDLE; }}
    long polls() const {{ return qin_.polls; }}

    void update() override {{
        cfg_.step(); qin_.step();
        if (phase_ == SEND_CFG && !cfg_.busy()) next();
        else if (phase_ == SEND_HDR && !qin_.busy()) {{ qin_.start(SCHEDULE[i_].samples); phase_ = SEND_SAMP; }}
        else if (phase_ == SEND_SAMP && !qin_.busy()) next();
        if (phase_ == IDLE && i_ < SCHEDULE.size()) {{
            const Item& it = SCHEDULE[i_];
            if (it.kind == CFG) {{ cfg_.start(it.words); phase_ = SEND_CFG; }}
            else                {{ qin_.start(it.words); phase_ = SEND_HDR; }}
        }}
    }}

private:
    enum {{ IDLE, SEND_CFG, SEND_HDR, SEND_SAMP }};
    void next() {{ ++i_; phase_ = IDLE; }}
    MmRegBankCfg cfg_;
    MmQueueWriter qin_;
    size_t i_ = 0;
    int phase_ = IDLE;
}};

/// Takes one output packet per input packet, then that packet's response, which it checks -- each on
/// its queue's interrupt, no polling; then reads the final status once (the kernel publishes it before
/// each response, so after the last response it is final).
class Reader : public XsiSimObj {{
public:
    Reader(AxiMmMaster& m, const IrqPin& qout_irq, const IrqPin& qresp_irq)
        : qout_(m, at(mm_fir_layout::qout, FIR), POLL), qresp_(m, at(mm_fir_layout::qresp, FIR), POLL),
          st_(m, at(mm_fir_layout::regs, FIR), POLL) {{ qout_.use_irq(qout_irq); qresp_.use_irq(qresp_irq); }}
    bool done() const {{ return phase_ == DONE; }}
    long polls() const {{ return qout_.polls + qresp_.polls; }}
    std::vector<uint64_t> y, final_status;
    long nresp = 0, mismatches = 0;

    void update() override {{
        qout_.step(); qresp_.step(); st_.step();
        if (phase_ == READ && !qout_.busy()) {{
            y.insert(y.end(), qout_.words.begin(), qout_.words.end());
            qresp_.start(1); phase_ = RESP;
        }}
        else if (phase_ == RESP && !qresp_.busy()) {{
            const Item& it = SCHEDULE[i_];
            ++nresp;
            if ({fld("qresp_.words", f_tx)} != it.tx || {fld("qresp_.words", f_seq)} != it.want) ++mismatches;
            ++i_; phase_ = IDLE;
        }}
        else if (phase_ == STATUS && !st_.busy()) {{ final_status = st_.words; phase_ = DONE; }}
        if (phase_ == IDLE) {{
            while (i_ < SCHEDULE.size() && SCHEDULE[i_].kind != PKT) ++i_;
            if (i_ < SCHEDULE.size()) {{ qout_.start(SCHEDULE[i_].nsamp); phase_ = READ; }}
            else {{ st_.start(0); phase_ = STATUS; }}
        }}
    }}

private:
    enum {{ IDLE, READ, RESP, STATUS, DONE }};
    MmQueueReader qout_, qresp_;
    MmStatusReader st_;
    size_t i_ = 0;
    int phase_ = IDLE;
}};

int main() {{
    XsiSim sim("{dll}", "mm_fir.wdb");
    AxiMmMaster host(sim.dut(), "s0_axi", 8, 0, /*overlap_rw=*/{"true" if OVERLAP_RW else "false"});
    IrqPin irq_qin(sim.dut(), "irq_qin"), irq_qout(sim.dut(), "irq_qout"), irq_qresp(sim.dut(), "irq_qresp");
    Reader rd(host, irq_qout, irq_qresp);   // the pysim reader runs first at t = 0 too (it is the host's run_proc)
    Writer wr(host, irq_qin);
    std::vector<XsiSimObj*> all = {{&irq_qin, &irq_qout, &irq_qresp, &host, &rd, &wr}};
    auto drive = [&] {{ for (auto* p : all) p->drive(); }};
    sim.reset(drive);
    long cyc = 0;
    auto finished = [&] {{ return rd.done() && wr.done(); }};
    for (; cyc < 200000 && !finished(); ++cyc) {{
        sim.clock_low();  for (auto* p : all) p->sample();
        sim.clock_high(); for (auto* p : all) p->update(); drive();
    }}
    std::printf("DONE done=%d cycles=%ld polls=%ld nops=%zu\\n", (int)finished(), cyc,
                rd.polls() + wr.polls(), host.nops());
    std::printf("RESP n=%ld mismatches=%ld\\n", rd.nresp, rd.mismatches);
    if (rd.done())
        std::printf("STATUS nsamp=%u ncfg=%u\\n", {fld("rd.final_status", f_nsamp)},
                    {fld("rd.final_status", f_ncfg)});
    for (size_t i = 0; i < host.nops(); ++i) {{
        const AxiMmMaster::Op& o = host.op(i);
        std::printf("OP %c 0x%llx n=%zu s=%ld e=%ld\\n", o.write ? 'W' : 'R', (unsigned long long)o.addr,
                    o.write ? o.wdata.size() : (size_t)o.nwords, o.t_start, o.t_end);
    }}
    std::printf("Y");
    for (uint64_t v : rd.y) std::printf(" %llx", (unsigned long long)v);
    std::printf("\\n");
    sim.close();
    return finished() ? 0 : 1;
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
               extra_files={"mm_fir_top.v": render_top("mm_fir_top", topology),
                            **address_headers()})
    return ws.run(timeout=timeout)
