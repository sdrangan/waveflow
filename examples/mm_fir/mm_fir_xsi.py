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
    MmFirSystem,
    S16,
    FirCmdHdr,
    FirRespHdr,
    FirStatus,
    HOST_MAX_OUTSTANDING,
    host_schedule,
    make_cfg,
)
from waveflow.hw.arrayutils import array
from waveflow.hw.mm_device import bus_address_headers
from waveflow.build.axi_xbar import AxiXbarConfig, generate_axi_xbar
from waveflow.build.mm_adaptor_gen import leaf_sources
from waveflow.build.system_top import SystemTopSpec, render_system_top, system_top_spec
from waveflow.build.xsi_workspace import XsiWorkspace

ROOT = Path(__file__).resolve().parent
RTL = ROOT / "mm_fir_proj" / "solution1" / "syn" / "verilog"

#: Two topologies, one address map (view k at REGS + k * 4 KB either way):
#:   per_view  -- a 1x4 crossbar, each view its own MI slot and its own front (Stages 1-2);
#:   one_front -- all four views behind ONE front and a generated decoder (Stage 4), on MI0 of a 1x2
#:                crossbar.  MI1 is a stub nothing addresses: a 1x1 crossbar is degenerate (create_ip
#:                generates an inconsistent 2-MI IP for it -- see AxiXbarConfig), and a real system has
#:                more than one slave anyway.
#: Their crossbars' IP names.  Nothing else about the top is written here: :func:`system_spec` walks
#: the pysim system (``waveflow.build.system_top``, ``plans/xsi_system_top.md`` S3) -- the crossbar's
#: ranges are where ``assign_address_ranges`` set them (``plans/bus_address_map.md`` D4).
XBAR_NAMES = {"per_view": "xbar_mm4_1x4", "one_front": "xbar_mm1_1x2"}

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


#: Timing probes: one-bit handshakes the top exposes as outputs when built with ``probes=True``; the
#: testbench samples them every cycle and prints the cycles each fired.  Off for the gate.  The nets
#: are the pysim system's stream channels, by name (``build_mm_device``: ``k_<view>``).
PROBES = {
    "in": "k_qin_TVALID && k_qin_TREADY",              # the kernel takes a word from queue in
    "cfg": "k_regs_cfg_TVALID && k_regs_cfg_TREADY",   # ... a config word
    "out": "k_qout_TVALID && k_qout_TREADY",           # a result into queue out
    "resp": "k_qresp_TVALID && k_qresp_TREADY",        # a response word
    "stat": "k_regs_stat_TVALID && k_regs_stat_TREADY",  # a status word
    "out_full": "k_qout_TVALID && !k_qout_TREADY",     # the kernel held up by a full queue out
}


def system_spec(topology: str) -> SystemTopSpec:
    """The RTL top for *topology*, walked from the pysim system: the kernel is the cut, so its device
    (views, adaptor) is inside, and the host's bus master and interrupt lines are top ports."""
    if topology not in XBAR_NAMES:
        raise ValueError(f"topology must be one of {sorted(XBAR_NAMES)}, got {topology!r}")
    sysm = MmFirSystem(x=[0], plan=PLAN, one_front=topology == "one_front")
    return system_top_spec(sysm.xbar, [sysm.fir], top="mm_fir_top", xbar_name=XBAR_NAMES[topology])


def xbar_config(topology: str) -> AxiXbarConfig:
    """The RTL crossbar for *topology*, generated from the pysim system's own crossbar -- the same
    slaves at the same ranges, so an address is written once (``MM_BASE`` + the type's layout)."""
    return system_spec(topology).xbar


def scenario_x() -> np.ndarray:
    return np.random.default_rng(7).integers(-2000, 2000, size=NSAMP)


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
    """The address-map headers the C++ host needs, found by walking the pysim system's crossbar
    (``bus_address_headers``): the FIR TYPE's layout (``mm_fir_layout.h``, from ``MmFir.mm_views``) and
    this SYSTEM's bases (``mm_fir_bases.h``).  The testbench combines them --
    ``at(mm_fir_layout::qin, FIR)`` -- and restates neither.  The same for both topologies: one front
    or one slot per view, the views sit at the same offsets."""
    return bus_address_headers(MmFirSystem(x=[0], plan=PLAN).xbar, system="mm_fir")


def render_tb(dll: str, x, probes: bool = False) -> str:
    """The C++ host: the pysim :class:`~examples.mm_fir.mm_fir.FirHost`, written against the C++
    endpoints of ``xsi_mm_host.h``.  Same two programs (a writer and a reader on one bus master), the
    same :func:`~examples.mm_fir.mm_fir.host_schedule`, the same polling rules, the same check of
    every response."""
    includes = "\n".join(f'#include "{h}"' for h in address_headers())

    def hexes(words):
        return ", ".join(f"0x{int(w):x}ull" for w in words)

    rows, tx = [], 0
    for item in host_schedule(len(x), PLAN, PKT):
        if item[0] == "cfg":
            rows.append(f"    {{CFG, {{{hexes(make_cfg(item[1], cfg_id=item[2]).serialize(word_bw=DW))}}}, {{}}, 0u, 0u, 0u}},")
        else:
            _, n0, n1, tag, want = item
            hdr = FirCmdHdr(nsamp=n1 - n0, tx_id=tx, cfg_id=tag).serialize(word_bw=DW)
            samples = array(S16, np.asarray(x[n0:n1], dtype=np.int64)).serialize(word_bw=DW)
            rows.append(f"    {{PKT, {{{hexes(hdr)}}}, {{{hexes(samples)}}}, {n1 - n0}u, {tx}u, {want}u}},")
            tx += 1
    names = list(PROBES) if probes else []
    probe_decl = "\n".join(f'    ProbePin pr_{n}(sim.dut(), "probe_{n}", "{n}");' for n in names)
    probe_list = "".join(f", &pr_{n}" for n in names)
    probe_dump = "\n".join(f"    pr_{n}.dump();" for n in names)
    f_nsamp = field_pos(FirStatus, "nsamp")
    f_ncfg = field_pos(FirStatus, "ncfg")
    f_tx = field_pos(FirRespHdr, "tx_id")
    f_seq = field_pos(FirRespHdr, "cfg_id")

    def fld(words: str, pos) -> str:
        return f"field({words}, {pos[0]}, {pos[1]}, {pos[2]})"

    return f'''// mm_fir_tb.cpp -- GENERATED by examples/mm_fir/mm_fir_xsi.py: host program -> crossbar ->
// adaptor -> mm_fir kernel.  The host is the pysim FirHost on the C++ endpoints of xsi_mm_host.h:
// a writer and a reader sharing one AxiMmMaster, and no address anywhere in this file.
#include "xsi_bfm.h"
#include "xsi_mm_host.h"
{includes}
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

/// A timing probe: a one-bit output of the top, sampled every cycle; dump() prints the cycles it was
/// high, as runs "start+len".
class ProbePin : public XsiSimObj {{
public:
    ProbePin(Dut& d, const char* port, const char* name) : d_(d), p_(d.port(port)), name_(name) {{}}
    void sample() override {{
        if (d_.get1(p_)) {{
            if (!runs_.empty() && runs_.back().first + runs_.back().second == cyc_) ++runs_.back().second;
            else runs_.push_back({{cyc_, 1}});
        }}
        ++cyc_;
    }}
    void dump() const {{
        std::printf("PROBE %s", name_);
        for (auto& r : runs_) std::printf(" %ld+%ld", r.first, r.second);
        std::printf("\\n");
    }}
private:
    Dut& d_; int p_; const char* name_; long cyc_ = 0;
    std::vector<std::pair<long, long> > runs_;
}};

static uint32_t field(const std::vector<uint64_t>& w, int word, int bit, int width) {{
    const uint64_t v = w[word] >> bit;
    return (uint32_t)(width >= 64 ? v : (v & ((1ull << width) - 1)));
}}

/// Commits each config; sends each packet as two queue-in packets -- its header, then its samples.
/// It never waits for a config to be received: the header's cfg_id makes the kernel wait.  It waits
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
{probe_decl}
    Reader rd(host, irq_qout, irq_qresp);   // the pysim reader runs first at t = 0 too (it is the host's run_proc)
    Writer wr(host, irq_qin);
    std::vector<XsiSimObj*> all = {{&irq_qin, &irq_qout, &irq_qresp, &host, &rd, &wr{probe_list}}};
    auto drive = [&] {{ for (auto* p : all) p->drive(); }};
    sim.reset(drive);
    long cyc = 0;
    auto finished = [&] {{ return rd.done() && wr.done(); }};
    for (; cyc < 200000 && !finished(); ++cyc) {{
        sim.clock_low();  for (auto* p : all) p->sample();
        sim.clock_high(); for (auto* p : all) p->update(); drive();
    }}
{probe_dump}
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


def probe_runs(out: str) -> dict[str, list[tuple[int, int]]]:
    """``{probe: [(start_cycle, length), ...]}`` from a ``probes=True`` run's PROBE lines."""
    res = {}
    for ln in out.splitlines():
        if ln.startswith("PROBE "):
            parts = ln.split()
            res[parts[1]] = [tuple(int(v) for v in r.split("+")) for r in parts[2:]]
    return res


def parse_kv(out: str, tag: str) -> dict[str, int]:
    line = next(ln for ln in out.splitlines() if ln.startswith(tag + " "))
    return {k: int(v) for k, v in (kv.split("=") for kv in line.split()[1:])}


def output_words(out: str) -> np.ndarray:
    """The outputs the host drained (the ``Y`` line), as signed int64."""
    y_line = next(ln for ln in out.splitlines() if ln.startswith("Y"))
    return np.array([np.int64(np.uint64(int(h, 16))) for h in y_line.split()[1:]], dtype=np.int64)


def run_xsi(topology: str, work_dir, timeout: int = 3600, probes: bool = False) -> str:
    """Generate the crossbar, render the top and the host program, and run XSI.  Returns the output.

    Needs Vivado (``create_ip`` + xsim) and the kernel's RTL (``python -m examples.mm_fir.mm_fir_build``).
    """
    if topology not in XBAR_NAMES:
        raise ValueError(f"topology must be one of {sorted(XBAR_NAMES)}, got {topology!r}")
    if not RTL.is_dir():
        raise FileNotFoundError(f"no csynth RTL at {RTL}: run python -m examples.mm_fir.mm_fir_build")
    work_dir = Path(work_dir)
    spec = system_spec(topology)
    ip = generate_axi_xbar(spec.xbar, work_dir / "ip")
    ws = XsiWorkspace(work_dir / f"mm_fir_{topology}{'_probes' if probes else ''}", top="mm_fir_top")
    ws.prepare(rtl_files=ip.sim_files + leaf_sources() + sorted(RTL.glob("*.v")) + ["mm_fir_top.v"],
               include_dirs=ip.include_dirs, tb_name="mm_fir_tb",
               tb_cpp=render_tb(ws.design_dll, scenario_x(), probes=probes),
               extra_files={"mm_fir_top.v": render_system_top(spec, PROBES if probes else None),
                            **address_headers()})
    return ws.run(timeout=timeout)
