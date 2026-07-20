// mem_r_bfm_tb.cpp — XSI cycle-based AXI-MM + AXIS BFM that RTL-verifies the generated free-running
// MemRStream kernel (gen/mem_r_stream.cpp) in xsim on Windows.
//
// The kernel is ap_ctrl_none (free-running); Vitis C/RTL cosim refuses it, so we drive the elaborated
// RTL directly through XSI.  Its three interfaces are modelled by the reusable BFM (xsi_bfm.h): an
// AXIS master on s_cmd, an AXIS slave on m_out, and an AXI-MM read slave on gmem0 serving one flat
// arena.  Gate: MemRStream bursts mem[BASE_W .. BASE_W+N) onto m_out, and the collected stream equals
// the memory region bit-exact.
//
// The scenario DATA is not stated here: the memory region, the command, and the golden output stream
// are burst bundles under vectors/, written by mem_stream_gen.py::write_mem_r_xsi_bundles.  The models
// load them in pre_sim and dump the collected output in post_sim (xsi_bundle.h), so the pattern lives
// once, in Python.
#include <cstdio>
#include <cstdint>
#include <string>
#include <vector>
#include "xsi_bfm.h"
#include "mem_r_stream_ports.h"      // GENERATED from the same TopSpec as the top's pragmas

using namespace wfbfm;
namespace ports = mem_r_stream_ports;

// Structural constants — MUST match write_mem_r_xsi_bundles (the DATA is loaded from vectors/).
static const int  N        = 128;                    // words the kernel bursts
static const int  MEM_DW   = 64;
static const int  BPW      = MEM_DW / 8;             // bytes per word = 8
static const int  MEM_NW   = 8192;
static const int  BASE_W   = 64;                     // region base (word index)
static const long MAX_CYCLES = 2000000L;

int main() {
    // 1) The participants.  Memory seeds itself from vectors/mem_in at word BASE_W; the command
    //    driver plays vectors/cmd; the output sink dumps what it collected to vectors/out.
    FlatMemory     mem(MEM_NW, BPW);
    XsiSim         sim(ports::DESIGN_DLL, "mem_r_bfm.wdb");
    sim.pin_low(ports::ZERO_PORTS, ports::ZERO_PORTS_N);

    AxisMaster     s_cmd(sim.dut(), ports::s_cmd, {});
    AxisSlave      m_out(sim.dut(), ports::m_out);
    AxiMmReadSlave gmem0(sim.dut(), ports::m_mem, mem);

    mem.load_segs    = { { (size_t)BASE_W, 0, "vectors/mem_in" } };
    s_cmd.in_bundle  = "vectors/cmd";
    m_out.out_bundle = "vectors/out";

    // Declaration order = the old sample/update/drive order (memory first, no-op on the cycle
    // phases) — so the schedule, and the cycle count, are unchanged from the hand-rolled loop.
    std::vector<XsiSimObj*> parts = { &mem, &s_cmd, &m_out, &gmem0 };

    for (auto* p : parts) p->pre_sim();          // seed memory + load the command before reset
    sim.reset([&]{ for (auto* p : parts) p->drive(); });

    // 2) Cycle loop.
    long cyc = 0, drain = -1; bool timed_out = false;
    for (;;) {
        if (drain >= 0 && (cyc - drain) >= 256) break;
        if ((int)m_out.count() >= N && drain < 0) drain = cyc;
        if (cyc >= MAX_CYCLES) { timed_out = true; break; }
        ++cyc;

        sim.clock_low();
        for (auto* p : parts) p->sample();
        sim.clock_high();
        for (auto* p : parts) p->update();
        for (auto* p : parts) p->drive();
    }

    for (auto* p : parts) p->post_sim();          // dump vectors/out

    const std::vector<uint64_t>& got = m_out.words();
    if (timed_out) {
        std::fprintf(stderr, "FAILED test: TIMEOUT cyc=%ld got=%zu/%d cmd_widx=%d/%d g_state=%d\n",
                     cyc, got.size(), N, s_cmd.sent(), s_cmd.total(), gmem0.state());
        sim.close();
        return 1;
    }

    // 3) Golden: the expected output stream is a bundle written from the same scenario, so the
    //    pattern is stated once (in Python), not re-implemented here.
    const std::vector<uint64_t> golden = BurstBundle::read_words("vectors/golden");
    int fails = 0;
    for (size_t i = 0; i < golden.size(); ++i) {
        uint64_t exp = golden[i];
        if (i >= got.size() || got[i] != exp) {
            if (fails < 8) std::fprintf(stderr, "  word %zu: got 0x%016llx exp 0x%016llx\n",
                                        i, (unsigned long long)(i < got.size() ? got[i] : 0),
                                        (unsigned long long)exp);
            ++fails;
        }
    }
    // `cycles` is time-to-last-word, NOT the loop count: the loop runs a fixed drain tail past
    // completion to let trailing bus activity settle, and that tail is a testbench constant with
    // nothing to do with the design.
    const long latency = (drain >= 0) ? drain : cyc;
    std::printf("mem_r_stream XSI BFM: N=%d collected=%zu cycles=%ld (tail=%ld)\n",
                N, got.size(), latency, cyc - latency);
    sim.close();
    if (fails || got.size() != golden.size()) {
        std::printf("FAILED test: %d mismatches (got %zu)\n", fails, got.size());
        return 1;
    }
    std::printf("PASSED test\n");
    return 0;
}
