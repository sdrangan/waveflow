// mem_w_bfm_tb.cpp — XSI cycle-based AXI-MM + AXIS BFM that RTL-verifies the generated free-running
// MemWStream kernel (gen/mem_w_stream.cpp) in xsim on Windows.  Mirror of mem_r_bfm_tb.cpp.
//
// Gate: MemWStream drains N known words off s_in and pure-writes them to mem[BASE_W .. BASE_W+N);
// the backing memory region equals the input bit-exact.  TB drives s_cmd (master), s_in (master),
// and the gmem0 AXI write slave (AWREADY / WREADY / B*); the read side + control are pinned to 0.
//
// The scenario DATA is not stated here: the command, the input stream, and the golden memory region
// are burst bundles under vectors/, written by mem_stream_gen.py::write_mem_w_xsi_bundles.  The
// drivers load them in pre_sim; the memory dumps the written region to vectors/out in post_sim.
#include <cstdio>
#include <cstdint>
#include <string>
#include <vector>
#include "xsi_bfm.h"
#include "mem_w_stream_ports.h"      // GENERATED from the same TopSpec as the top's pragmas

using namespace wfbfm;
namespace ports = mem_w_stream_ports;

// Structural constants — MUST match write_mem_w_xsi_bundles (the DATA is loaded from vectors/).
static const int  N        = 128;
static const int  MEM_DW   = 64;
static const int  BPW      = MEM_DW / 8;
static const int  MEM_NW   = 8192;
static const int  BASE_W   = 64;
static const long MAX_CYCLES = 2000000L;

int main() {
    // Participants.  The command + data drivers play vectors/cmd + vectors/dat; the memory dumps the
    // region it was written to (vectors/out) in post_sim.
    FlatMemory      mem(MEM_NW, BPW);
    XsiSim          sim(ports::DESIGN_DLL, "mem_w_bfm.wdb");
    sim.pin_low(ports::ZERO_PORTS, ports::ZERO_PORTS_N);

    AxisMaster      s_cmd(sim.dut(), ports::s_cmd, {});
    AxisMaster      s_in (sim.dut(), ports::s_in,  {});
    AxiMmWriteSlave gmem0(sim.dut(), ports::m_mem, mem);

    s_cmd.in_bundle = "vectors/cmd";
    s_in.in_bundle  = "vectors/dat";
    mem.dump_segs   = { { (size_t)BASE_W, (size_t)N, "vectors/out" } };

    // Declaration order = the old sample/update/drive order (memory first, no-op on the cycle phases).
    std::vector<XsiSimObj*> parts = { &mem, &s_cmd, &s_in, &gmem0 };

    for (auto* p : parts) p->pre_sim();          // load command + input stream before reset
    sim.reset([&]{ for (auto* p : parts) p->drive(); });

    long cyc = 0, drain = -1; bool timed_out = false;
    for (;;) {
        if (drain >= 0 && (cyc - drain) >= 256) break;
        // Drain only once the write is ACKNOWLEDGED, not merely sent: w_count alone would let the
        // run stop before the last burst's B response.
        if (gmem0.w_count() >= N && gmem0.saw_b() && drain < 0) drain = cyc;
        if (cyc >= MAX_CYCLES) { timed_out = true; break; }
        ++cyc;

        sim.clock_low();
        for (auto* p : parts) p->sample();
        sim.clock_high();
        for (auto* p : parts) p->update();
        for (auto* p : parts) p->drive();
    }

    for (auto* p : parts) p->post_sim();          // dump mem[BASE_W..BASE_W+N) to vectors/out

    if (timed_out) {
        std::fprintf(stderr, "FAILED test: TIMEOUT cyc=%ld w_count=%d/%d in_idx=%d cmd_widx=%d/%d g_state=%d\n",
                     cyc, gmem0.w_count(), N, s_in.sent(), s_cmd.sent(), s_cmd.total(), gmem0.state());
        sim.close();
        return 1;
    }

    // Golden: the expected written region is a bundle (= the input stream), so the pattern is stated
    // once, in Python.
    const std::vector<uint64_t> golden = BurstBundle::read_words("vectors/golden");
    int fails = 0;
    for (size_t i = 0; i < golden.size(); ++i) {
        uint64_t exp = golden[i], got = mem[BASE_W + (int)i];
        if (got != exp) {
            if (fails < 8) std::fprintf(stderr, "  word %zu: got 0x%016llx exp 0x%016llx\n",
                                        i, (unsigned long long)got, (unsigned long long)exp);
            ++fails;
        }
    }
    // `cycles` is time-to-last-completion, NOT the loop count — see the note in mem_r_bfm_tb.cpp.
    const long latency = (drain >= 0) ? drain : cyc;
    std::printf("mem_w_stream XSI BFM: N=%d w_count=%d cycles=%ld (tail=%ld)\n",
                N, gmem0.w_count(), latency, cyc - latency);
    sim.close();
    if (fails) { std::printf("FAILED test: %d mismatched words\n", fails); return 1; }
    std::printf("PASSED test\n");
    return 0;
}
