"""markov_xsi.py — the Markov system at RTL: four bus masters, one crossbar, two kernels, under XSI.

Stage 4 of ``plans/mm_credit_stream.md``.  Everything is real RTL under one generated Verilog top:

* AMD's ``axi_crossbar``, generated from the pysim system's own crossbar (``AxiXbarConfig.from_crossbar``)
  -- 4 SI: the host (the testbench's ``AxiMmMaster``), the generator's queue writer, the chain's credit
  writer, the chain's memory writer;  3 MI: the generator's adaptor (``qcmd`` queue in, ``u_crd``
  credit in), the chain's adaptor (``qu`` queue in, ``qresp`` queue out), and the shared memory -- a
  BRAM window behind its own front, which echoes AXI IDs as a four-master crossbar needs;
* the four csynth'd tops: ``markov_gen``, ``markov_chain`` (its core + the in-band memory writer),
  the queue writer and the credit writer (``waveflow/build/mm_writer_gen.py``).  Each writer's ``target`` -- its peer view's bus
  word index -- is a constant the top drives, so neither kernel's RTL depends on placement.

The top is wired from the csynth'd modules' own port lists (:func:`module_ports`), not from a table of
pin names: an ``m_axi`` pin the crossbar has is joined to its SI slot, one it lacks is tied off (an
input) or left open (an output), and every ``s_axi_control`` input is tied low -- the ``m_axi`` base
register stays 0, so a bus address is the address.

The host (:func:`render_tb`) is the pysim :class:`~examples.markov.markov.MarkovHost` on the C++
endpoints of ``xsi_mm_host.h``: a writer that sends each command on ``qcmd`` (room interrupt) while at
most ``MAX_IN_FLIGHT`` jobs are out, and a reader that takes each response on ``qresp`` (data
interrupt) and then reads that job's ``x`` from the memory.  Nothing polls.
"""
from __future__ import annotations

import re
from pathlib import Path

import numpy as np

from examples.markov.markov import (
    CDEPTH,
    CHAIN_LAYOUT,
    CHAIN_BASE,
    DW,
    GEN_BASE,
    GEN_LAYOUT,
    MAX_IN_FLIGHT,
    MEM_BASE,
    QDEPTH,
    RDEPTH,
    REGION_BYTES,
    U8,
    MarkovSystem,
    MkvCmd,
    MkvResp,
    default_jobs,
)
from waveflow.build.axi_xbar import (
    AxiXbarConfig,
    axi_port_decls,
    axi_signals,
    axi_wire_decls,
    generate_axi_xbar,
    render_xbar_instance,
)
from waveflow.build.mm_adaptor_gen import (
    BramView,
    CreditInView,
    QueueView,
    leaf_sources,
    mi_wire_signals,
    render_adaptor_slot,
    render_view_slot,
)
from waveflow.build.mm_writer_gen import writer_top_name
from waveflow.build.xsi_workspace import XsiWorkspace
from waveflow.hw.arrayutils import get_nwords
from waveflow.hw.mm_device import bus_address_headers

ROOT = Path(__file__).resolve().parent
#: The four csynth'd tops.  The writers' names are derived as markov_build derives them (the queue
#: writer's longest packet is the queue depth), never restated.
QWRITER = writer_top_name("queue", DW, QDEPTH)
CWRITER = writer_top_name("credit", DW)
TOPS = ("markov_gen", "markov_chain", QWRITER, CWRITER)


def rtl_dir(top: str) -> Path:
    return ROOT / f"{top}_proj" / "solution1" / "syn" / "verilog"


XBAR_NAME = "xbar_markov_4x3"
#: Four masters need two ID bits to route responses back.
ID_WIDTH = 2
#: The testbench scenario: four jobs of 300 steps (two in flight at a time).
NJOBS, NSTEPS = 4, 300
POLL = 8

GEN_VIEWS = [QueueView("qcmd", "in", axis="k_cmd", depth=CDEPTH), CreditInView("u_crd", axis="k_ucrd")]
CHAIN_VIEWS = [QueueView("qu", "in", axis="k_u", depth=QDEPTH),
               QueueView("qresp", "out", axis="k_resp", depth=RDEPTH)]
MEM_VIEW = BramView("mem", kport="memb", baw=9)            # 512 words = the 4 KB window


#: Timing probes (plans: markov-timing): one-bit handshakes the top exposes as outputs when built with
#: ``probes=True``, and the testbench samples every cycle.  Off for the gate.
PROBES = {
    "cmd": "k_cmd_TVALID && k_cmd_TREADY",                  # host's command reaches the generator
    "ufwd": "k_ufwd_TVALID && k_ufwd_TREADY",               # generator -> its queue writer, a word
    "ufwd_last": "k_ufwd_TVALID && k_ufwd_TREADY && k_ufwd_TLAST",
    "wr1_aw": "si1_axi_AWVALID && si1_axi_AWREADY",          # queue writer: a burst issued
    "wr1_b": "si1_axi_BVALID && si1_axi_BREADY",             # ... and acknowledged
    "u": "k_u_TVALID && k_u_TREADY",                         # chain takes a word from its queue in
    "crd": "k_crd_TVALID && k_crd_TREADY",                   # chain offers credit
    "wr2_aw": "si2_axi_AWVALID && si2_axi_AWREADY",          # credit writer: a credit write
    "ucrd": "k_ucrd_TVALID && k_ucrd_TREADY",                # generator takes a credit value
    "wr3_aw": "si3_axi_AWVALID && si3_axi_AWREADY",          # chain's memory writer: a burst
    "wr3_b": "si3_axi_BVALID && si3_axi_BREADY",
    "resp": "k_resp_TVALID && k_resp_TREADY",                # a response word into qresp
}


def scenario_jobs() -> list[dict]:
    return default_jobs(NJOBS, NSTEPS)


def system() -> MarkovSystem:
    return MarkovSystem(jobs=scenario_jobs(), link="mm")


def xbar_config() -> AxiXbarConfig:
    """The RTL crossbar, generated from the pysim crossbar: the same slaves at the same ranges."""
    return AxiXbarConfig.from_crossbar(system().xbar, XBAR_NAME, id_width=ID_WIDTH)


def module_ports(top: str) -> list[tuple[str, str]]:
    """``(direction, name)`` of every port of the csynth'd module *top*, from its Verilog."""
    src = (rtl_dir(top) / f"{top}.v").read_text(encoding="utf-8", errors="replace")
    body = src[src.index(f"module {top}"):]
    body = body[:body.index("endmodule")]
    return [(m[1], m[2]) for m in re.finditer(r"^\s*(input|output)\s+(?:wire\s+)?(?:\[[^\]]*\]\s*)?(\w+)\s*;",
                                              body, re.M)]


_STREAM_SUFFIXES = ("TDATA", "TVALID", "TREADY", "TLAST", "TKEEP", "TSTRB")


def _stream_wires(net: str) -> list[str]:
    return [f"  wire [{DW - 1}:0] {net}_TDATA;", f"  wire {net}_TVALID, {net}_TREADY, {net}_TLAST;",
            f"  wire [{DW // 8 - 1}:0] {net}_TKEEP, {net}_TSTRB;"]


def _instance(top: str, inst: str, streams: dict[str, str], si: str | None = None,
              target: int | None = None) -> str:
    """Instantiate *top*, connecting each port by rule: clock/reset; a stream port group to the net
    ``streams[<port prefix>]``; ``m_axi_gmem0_*`` to the SI wires *si* (IDs: ours are driven 0 outside,
    the kernel's BID/RID take bit 0; a pin the crossbar lacks is tied 0 / left open); every
    ``s_axi_control`` input tied 0; ``target`` to the constant."""
    si_sigs = {name for name, _w, _m in axi_signals(DW, 32, ID_WIDTH)}
    conns = []
    for d, name in module_ports(top):
        if name in ("ap_clk", "ap_rst_n"):
            conns.append(f".{name}({name})")
        elif name == "target":
            conns.append(f".target(32'd{int(target)})")
        elif name.startswith("s_axi_control_"):
            conns.append(f".{name}({'0' if d == 'input' else ''})")
        elif name.startswith("m_axi_gmem0_"):
            sig = name[len("m_axi_gmem0_"):]
            if si is None or sig not in si_sigs:
                conns.append(f".{name}({'0' if d == 'input' else ''})")
            elif sig in ("AWID", "ARID"):
                conns.append(f".{name}()")                    # driven 0 on the SI side instead
            elif sig in ("BID", "RID"):
                conns.append(f".{name}({si}_{sig}[0])")
            else:
                conns.append(f".{name}({si}_{sig})")
        else:
            for prefix, net in streams.items():
                if name.startswith(prefix + "_") and name[len(prefix) + 1:] in _STREAM_SUFFIXES:
                    conns.append(f".{name}({net}_{name[len(prefix) + 1:]})")
                    break
            else:
                conns.append(f".{name}({'0' if d == 'input' else ''})")    # interrupt etc.
    return f"  {top} {inst} (\n    " + ",\n    ".join(conns) + "\n  );"


def render_top(top: str = "markov_top", probes: bool = False) -> str:
    xbar = xbar_config()
    dw, aw, idw = xbar.data_width, xbar.addr_width, xbar.id_width
    gmap, cmap = GEN_LAYOUT.at(GEN_BASE), CHAIN_LAYOUT.at(CHAIN_BASE)
    bpw = DW // 8
    ports = ["input wire ap_clk", "input wire ap_rst_n"]
    ports += axi_port_decls("s0_axi", axi_signals(dw, aw, idw), facing="slave")
    ports += ["output wire irq_qcmd", "output wire irq_qresp"]
    if probes:
        ports += [f"output wire probe_{n}" for n in PROBES]
    si = ["s0_axi", "si1_axi", "si2_axi", "si3_axi"]
    mi = ["mi0_axi", "mi1_axi", "mi2_axi"]
    body = []
    for p in si[1:]:
        body += ["  " + d for d in axi_wire_decls(p, axi_signals(dw, aw, idw))]
        body += [f"  assign {p}_AWID = 0;", f"  assign {p}_ARID = 0;"]
    for p in mi:
        body += ["  " + d for d in axi_wire_decls(p, mi_wire_signals(dw, aw, idw))]
    for net in ("k_cmd", "k_ucrd", "k_ufwd", "k_wr1", "k_u", "k_crd", "k_resp"):
        body += _stream_wires(net)
    # The forward FIFO: generator -> FIFO -> queue writer, at the depth the pysim channel declares (the
    # writer is store-and-forward; without it the generator stalls for every burst).
    depth = int(system().u_link.fwd_depth)
    body += [
        f"  wire [{dw}:0] ufifo_dout; wire ufifo_full, ufifo_empty; wire [$clog2({depth}):0] ufifo_count;",
        f"  mm_sync_fifo #(.W({dw + 1}), .DEPTH({depth})) u_ufifo (",
        "    .clk(ap_clk), .rst_n(ap_rst_n),",
        "    .push(k_ufwd_TVALID && !ufifo_full), .din({k_ufwd_TLAST, k_ufwd_TDATA}),",
        "    .pop(k_wr1_TVALID && k_wr1_TREADY), .dout(ufifo_dout),",
        "    .full(ufifo_full), .empty(ufifo_empty), .count(ufifo_count));",
        "  assign k_ufwd_TREADY = !ufifo_full;",
        "  assign k_wr1_TVALID = !ufifo_empty;",
        f"  assign k_wr1_TDATA = ufifo_dout[{dw - 1}:0];",
        f"  assign k_wr1_TLAST = ufifo_dout[{dw}];",
        f"  assign k_wr1_TKEEP = {{{dw // 8}{{1'b1}}}};",
        f"  assign k_wr1_TSTRB = {{{dw // 8}{{1'b1}}}};",
    ]
    # The queue out reads a TLAST the chain's response port does not have (it is unframed).
    body.append("  assign k_resp_TLAST = 1'b0;")
    body.append(render_xbar_instance(xbar, "u_xbar", si, mi))
    body.append(render_adaptor_slot("gen_mm", GEN_VIEWS, mi[0], dw, aw, idw))
    body.append(render_adaptor_slot("chain_mm", CHAIN_VIEWS, mi[1], dw, aw, idw))
    # The shared memory: a BRAM window whose kernel port (B) nothing uses.
    body += ["  wire [31:0] memb_addr = 0; wire memb_en = 1'b0; wire [1:0] memb_we = 2'b0;",
             f"  wire [{dw - 1}:0] memb_din = 0; wire [{dw - 1}:0] memb_dout;"]
    body.append(render_view_slot(MEM_VIEW, mi[2], dw, aw, idw))
    body += ["  assign irq_qcmd = qcmd_irq;", "  assign irq_qresp = qresp_irq;"]
    if probes:
        body += [f"  assign probe_{n} = {e};" for n, e in PROBES.items()]
    body.append(_instance("markov_gen", "u_gen",
                          {"s_cmd": "k_cmd", "m_u_fwd": "k_ufwd", "m_u_crd": "k_ucrd"}))
    body.append(_instance("markov_chain", "u_chain",
                          {"s_u_fwd": "k_u", "s_u_crd": "k_crd", "m_resp": "k_resp"}, si="si3_axi"))
    body.append(_instance(QWRITER, "u_fwd_wr", {"s_in": "k_wr1"}, si="si1_axi",
                          target=cmap["qu"].base // bpw))
    body.append(_instance(CWRITER, "u_crd_wr", {"s_in": "k_crd"}, si="si2_axi",
                          target=gmap["u_crd"].base // bpw))
    return (f"// {top}.v -- GENERATED by examples/markov/markov_xsi.py.\n"
            f"`timescale 1ns/1ps\nmodule {top} (\n  " + ",\n  ".join(ports) + "\n);\n"
            + "\n".join(body) + "\nendmodule\n")


def address_headers() -> dict[str, str]:
    """The headers the C++ host needs, found by walking the pysim crossbar: the two kernel types'
    layouts and this system's bases (the memory included)."""
    return bus_address_headers(system().xbar, system="markov")


def field_pos(schema, name: str) -> tuple[int, int, int]:
    """(word, bit, width) of *name* in *schema*'s 64-bit serialization, read off its serializer."""
    words = np.asarray(schema(**{name: 1}).serialize(word_bw=DW), dtype=np.uint64)
    width = int(schema.elements[name]["schema"].bitwidth)
    for i, w in enumerate(words):
        if int(w):
            return i, int(w).bit_length() - 1, width
    raise AssertionError(name)


def render_tb(dll: str, probes: bool = False) -> str:
    jobs = scenario_jobs()
    includes = "\n".join(f'#include "{h}"' for h in address_headers())
    rows = []
    for j, job in enumerate(jobs):
        cmd = MkvCmd(**job, dstaddr=MEM_BASE + j * REGION_BYTES).serialize(word_bw=DW)
        nx = get_nwords(U8, word_bw=DW, shape=job["n"])
        rows.append(f"    {{{{{', '.join(f'0x{int(w):x}ull' for w in cmd)}}}, "
                    f"0x{MEM_BASE + j * REGION_BYTES:x}ull, {nx}u}},")
    probe_names = list(PROBES) if probes else []
    probe_decl = "\n".join(
        f'    ProbePin pr_{n}(sim.dut(), "probe_{n}", "{n}");' for n in probe_names)
    probe_list = "".join(f", &pr_{n}" for n in probe_names)
    probe_dump = "\n".join(f"    pr_{n}.dump();" for n in probe_names)
    f_tx = field_pos(MkvResp, "tx_id")
    f_ones = field_pos(MkvResp, "ones")
    return f'''// markov_tb.cpp -- GENERATED by examples/markov/markov_xsi.py: the host program -> crossbar ->
// two kernels joined by a routed credit stream -> memory.  The host is the pysim MarkovHost on the
// C++ endpoints of xsi_mm_host.h; it names no address but the memory regions it hands out.
#include "xsi_bfm.h"
#include "xsi_mm_host.h"
{includes}
using namespace wfbfm;

/// One job: its MkvCmd words, the bus address its x lands at, and how many words x is.
struct Job {{ std::vector<uint64_t> cmd; uint64_t xaddr; uint32_t xwords; }};
static const std::vector<Job> JOBS = {{
{chr(10).join(rows)}
}};
static const long POLL = {POLL};
static const int MAX_IN_FLIGHT = {MAX_IN_FLIGHT};
static const uint64_t GEN = markov_bases::GEN_BASE, CHAIN = markov_bases::CHAIN_BASE;

static uint32_t field(const std::vector<uint64_t>& w, int word, int bit, int width) {{
    const uint64_t v = w[word] >> bit;
    return (uint32_t)(width >= 64 ? v : (v & ((1ull << width) - 1)));
}}

static int in_flight = 0;

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

/// Sends each job's command on qcmd -- room on qcmd's interrupt -- once fewer than MAX_IN_FLIGHT are out.
class Writer : public XsiSimObj {{
public:
    Writer(AxiMmMaster& m, const IrqPin& irq) : q_(m, at(markov_gen_layout::qcmd, GEN), POLL) {{
        q_.use_irq(irq);
    }}
    bool done() const {{ return i_ >= JOBS.size() && !q_.busy(); }}
    long polls() const {{ return q_.polls; }}
    void update() override {{
        q_.step();
        if (!q_.busy() && i_ < JOBS.size() && in_flight < MAX_IN_FLIGHT) {{
            q_.start(JOBS[i_].cmd); ++in_flight; ++i_;
        }}
    }}
private:
    MmQueueWriter q_;
    size_t i_ = 0;
}};

/// Takes each response on qresp -- data on qresp's interrupt -- then reads that job's x from memory.
class Reader : public XsiSimObj {{
public:
    Reader(AxiMmMaster& m, const IrqPin& irq) : m_(m), q_(m, at(markov_chain_layout::qresp, CHAIN), POLL) {{
        q_.use_irq(irq);
    }}
    bool done() const {{ return phase_ == DONE; }}
    long polls() const {{ return q_.polls; }}
    std::vector<std::vector<uint64_t> > x = std::vector<std::vector<uint64_t> >(JOBS.size());
    std::vector<uint32_t> ones = std::vector<uint32_t>(JOBS.size());
    std::vector<long> t_done = std::vector<long>(JOBS.size());
    void update() override {{
        q_.step();
        if (phase_ == RESP && !q_.busy()) {{
            tx_ = {"field(q_.words, %d, %d, %d)" % f_tx};
            ones[tx_] = {"field(q_.words, %d, %d, %d)" % f_ones};
            op_ = m_.read(JOBS[tx_].xaddr, JOBS[tx_].xwords, m_.cycle());
            phase_ = READX;
        }} else if (phase_ == READX && m_.op(op_).done()) {{
            x[tx_] = m_.op(op_).rdata; t_done[tx_] = m_.cycle(); --in_flight; ++n_;
            phase_ = IDLE;
        }}
        if (phase_ == IDLE) {{
            if (n_ < JOBS.size()) {{ q_.start({MkvResp.nwords_per_inst(DW)}); phase_ = RESP; }}
            else phase_ = DONE;
        }}
    }}
private:
    enum {{ IDLE, RESP, READX, DONE }};
    AxiMmMaster& m_;
    MmQueueReader q_;
    size_t op_ = 0, n_ = 0;
    uint32_t tx_ = 0;
    int phase_ = IDLE;
}};

int main() {{
    XsiSim sim("{dll}", "markov.wdb");
    AxiMmMaster host(sim.dut(), "s0_axi", 8, 0, /*overlap_rw=*/true);
    IrqPin irq_qcmd(sim.dut(), "irq_qcmd"), irq_qresp(sim.dut(), "irq_qresp");
{probe_decl}
    Reader rd(host, irq_qresp);
    Writer wr(host, irq_qcmd);
    std::vector<XsiSimObj*> all = {{&irq_qcmd, &irq_qresp, &host, &rd, &wr{probe_list}}};
    auto drive = [&] {{ for (auto* p : all) p->drive(); }};
    sim.reset(drive);
    long cyc = 0;
    auto finished = [&] {{ return rd.done() && wr.done(); }};
    for (; cyc < 400000 && !finished(); ++cyc) {{
        sim.clock_low();  for (auto* p : all) p->sample();
        sim.clock_high(); for (auto* p : all) p->update(); drive();
    }}
{probe_dump}
    std::printf("DONE done=%d cycles=%ld polls=%ld nops=%zu\\n", (int)finished(), cyc,
                rd.polls() + wr.polls(), host.nops());
    for (size_t j = 0; j < JOBS.size(); ++j) {{
        std::printf("JOB %zu ones=%u t=%ld X", j, rd.ones[j], rd.t_done[j]);
        for (uint64_t v : rd.x[j]) std::printf(" %llx", (unsigned long long)v);
        std::printf("\\n");
    }}
    for (size_t i = 0; i < host.nops(); ++i) {{
        const AxiMmMaster::Op& o = host.op(i);
        std::printf("OP %c 0x%llx n=%zu s=%ld e=%ld\\n", o.write ? 'W' : 'R', (unsigned long long)o.addr,
                    o.write ? o.wdata.size() : (size_t)o.nwords, o.t_start, o.t_end);
    }}
    sim.close();
    return finished() ? 0 : 1;
}}
'''


def parse_kv(out: str, tag: str) -> dict[str, int]:
    line = next(ln for ln in out.splitlines() if ln.startswith(tag + " "))
    return {k: int(v) for k, v in (kv.split("=") for kv in line.split()[1:] if "=" in kv)}


def probe_runs(out: str) -> dict[str, list[tuple[int, int]]]:
    """``{probe: [(start_cycle, length), ...]}`` from a ``probes=True`` run's PROBE lines."""
    res = {}
    for ln in out.splitlines():
        if ln.startswith("PROBE "):
            parts = ln.split()
            res[parts[1]] = [tuple(int(v) for v in r.split("+")) for r in parts[2:]]
    return res


def job_results(out: str) -> dict[int, dict]:
    """Per job: ``ones``, completion cycle ``t``, and ``x`` decoded from the words the host read."""
    from waveflow.hw.arrayutils import read_array

    res = {}
    jobs = scenario_jobs()
    for ln in out.splitlines():
        if not ln.startswith("JOB "):
            continue
        head, xs = ln.split(" X")
        parts = head.split()
        j = int(parts[1])
        kv = dict(p.split("=") for p in parts[2:])
        words = np.array([int(h, 16) for h in xs.split()], dtype=np.uint64)
        x = np.asarray(read_array(words, U8, word_bw=DW, shape=jobs[j]["n"]).val, dtype=np.uint8)
        res[j] = {"ones": int(kv["ones"]), "t": int(kv["t"]), "x": x}
    return res


def run_xsi(work_dir, timeout: int = 3600, probes: bool = False) -> str:
    """Generate the crossbar, render the top and the host program, and run XSI.  Needs Vivado and the
    four csynth'd tops (``python -m examples.markov.markov_build``)."""
    for t in TOPS:
        if not rtl_dir(t).is_dir():
            raise FileNotFoundError(f"no csynth RTL for {t}: run python -m examples.markov.markov_build")
    work_dir = Path(work_dir).resolve()        # Vivado runs in the IP directory: no relative paths
    ip = generate_axi_xbar(xbar_config(), work_dir / "ip")
    ws = XsiWorkspace(work_dir / ("markov_probes" if probes else "markov"), top="markov_top")
    rtl = [f for t in TOPS for f in sorted(rtl_dir(t).glob("*.v"))]
    ws.prepare(rtl_files=ip.sim_files + leaf_sources() + rtl + ["markov_top.v"],
               include_dirs=ip.include_dirs, tb_name="markov_tb",
               tb_cpp=render_tb(ws.design_dll, probes=probes),
               extra_files={"markov_top.v": render_top("markov_top", probes=probes),
                            **address_headers()})
    return ws.run(timeout=timeout)


if __name__ == "__main__":
    import sys
    out = run_xsi(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "xsi_work")
    print(out[-4000:])
