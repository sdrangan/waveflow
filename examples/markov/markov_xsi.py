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

The top is not written here: :func:`system_spec` walks the pysim system
(:mod:`waveflow.build.system_top`, ``plans/xsi_system_top.md`` S3) with the two kernels and the memory
as the cut.  Each kernel's pins come from the same ``TopSpec`` its csynth top was built from, and the
tie-off rules (``s_axi_control`` low, crossbar-less ``m_axi`` pins, IDs) are the framework's.

The host (:func:`render_tb`) is the pysim :class:`~examples.markov.markov.MarkovHost` on the C++
endpoints of ``xsi_mm_host.h``: a writer that sends each command on ``qcmd`` (room interrupt) while at
most ``MAX_IN_FLIGHT`` jobs are out, and a reader that takes each response on ``qresp`` (data
interrupt) and then reads that job's ``x`` from the memory.  Nothing polls.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from examples.markov.markov import (
    DW,
    MAX_IN_FLIGHT,
    MEM_BASE,
    QDEPTH,
    REGION_BYTES,
    U8,
    MarkovSystem,
    MkvCmd,
    MkvResp,
    default_jobs,
)
from waveflow.build.axi_xbar import AxiXbarConfig, generate_axi_xbar
from waveflow.build.mm_adaptor_gen import leaf_sources
from waveflow.build.mm_writer_gen import writer_top_name
from waveflow.build.system_top import SystemTopSpec, render_system_top, system_top_spec
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
#: The testbench scenario: four jobs of 300 steps (two in flight at a time).
NJOBS, NSTEPS = 4, 300
POLL = 8

#: Timing probes (plans: markov-timing): one-bit handshakes the top exposes as outputs when built with
#: ``probes=True``, and the testbench samples every cycle.  Off for the gate.  The nets are the pysim
#: system's channels, by name: a view's ``<kernel>_k_<view>``, the credit link's ``u_fwd`` (``_q`` past
#: its FIFO) and ``u_crd``; the SI slots are the crossbar's master order.
PROBES = {
    "cmd": "gen_k_qcmd_TVALID && gen_k_qcmd_TREADY",        # host's command reaches the generator
    "ufwd": "u_fwd_TVALID && u_fwd_TREADY",                 # generator -> its queue writer, a word
    "ufwd_last": "u_fwd_TVALID && u_fwd_TREADY && u_fwd_TLAST",
    "wr1_aw": "si1_axi_AWVALID && si1_axi_AWREADY",          # queue writer: a burst issued
    "wr1_b": "si1_axi_BVALID && si1_axi_BREADY",             # ... and acknowledged
    "u": "chain_k_qu_TVALID && chain_k_qu_TREADY",           # chain takes a word from its queue in
    "crd": "u_crd_TVALID && u_crd_TREADY",                   # chain offers credit
    "wr2_aw": "si2_axi_AWVALID && si2_axi_AWREADY",          # credit writer: a credit write
    "ucrd": "gen_k_u_crd_TVALID && gen_k_u_crd_TREADY",      # generator takes a credit value
    "wr3_aw": "si3_axi_AWVALID && si3_axi_AWREADY",          # chain's memory writer: a burst
    "wr3_b": "si3_axi_BVALID && si3_axi_BREADY",
    "resp": "chain_k_qresp_TVALID && chain_k_qresp_TREADY",  # a response word into qresp
}


def scenario_jobs() -> list[dict]:
    return default_jobs(NJOBS, NSTEPS)


def system() -> MarkovSystem:
    return MarkovSystem(jobs=scenario_jobs(), link="mm")


def system_spec() -> SystemTopSpec:
    """The RTL top, walked from the pysim system with the two kernels and the shared memory as the cut:
    their adaptors and the credit link's two writers come with them; the host is outside."""
    sysm = system()
    return system_top_spec(sysm.xbar, [sysm.gen, sysm.chain, sysm.mem], top="markov_top",
                           xbar_name=XBAR_NAME)


def xbar_config() -> AxiXbarConfig:
    """The RTL crossbar, generated from the pysim crossbar: the same slaves at the same ranges."""
    return system_spec().xbar


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
    irq = {view: port for port, view in system_spec().irqs}
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
    IrqPin irq_qcmd(sim.dut(), "{irq["gen_qcmd"]}"), irq_qresp(sim.dut(), "{irq["chain_qresp"]}");
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
    spec = system_spec()
    ip = generate_axi_xbar(spec.xbar, work_dir / "ip")
    ws = XsiWorkspace(work_dir / ("markov_probes" if probes else "markov"), top="markov_top")
    rtl = [f for t in spec.modules for f in sorted(rtl_dir(t).glob("*.v"))]
    ws.prepare(rtl_files=ip.sim_files + leaf_sources() + rtl + ["markov_top.v"],
               include_dirs=ip.include_dirs, tb_name="markov_tb",
               tb_cpp=render_tb(ws.design_dll, probes=probes),
               extra_files={"markov_top.v": render_system_top(spec, PROBES if probes else None),
                            **address_headers()})
    return ws.run(timeout=timeout)


if __name__ == "__main__":
    import sys
    out = run_xsi(Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "xsi_work")
    print(out[-4000:])
