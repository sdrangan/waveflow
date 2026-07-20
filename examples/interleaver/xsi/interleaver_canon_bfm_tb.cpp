// interleaver_canon_bfm_tb.cpp — XSI BFM for the CANONICAL six-stage interleaver
// (gen/interleaver_canon.cpp): cmd_rx -> il_mem_r -> il_load -> il_compute -> il_store -> il_mem_w,
// with one InterleaverCmd token per job forwarded through every stage (sob3's per-job pacing).  The
// definitive test that token forwarding breaks the done==#tasks+1 deadlock the mix (7/8) and P-SOB
// (6/8) variants hit.  s_done carries the 2-word InterleaverCmd token per job (one job-done per 2
// s_done beats).  ap_ctrl_none: driven directly through XSI (Vitis cosim is unreliable).
//
// The scenario DATA is not stated here: the input arena (P + X, lane-packed), the InterleaverCmd
// stream, and the golden output arena (Y[i] = X[P[i]]) are burst bundles under vectors/, written by
// mem_stream_gen.py::write_interleaver_canon_xsi_bundles.  The memory + command load in pre_sim; the
// memory dumps its arena to vectors/out in post_sim.
#include <cstdio>
#include <cstdint>
#include <string>
#include <vector>
#include "xsi_bfm.h"
#include "interleaver_canon_ports.h"      // GENERATED from the same TopSpec as the top's pragmas

using namespace wfbfm;
namespace ports = interleaver_canon_ports;

// Structural constants — MUST match write_interleaver_canon_xsi_bundles (DATA is loaded from vectors/).
static const int  N        = 256;
static const int  NJ       = 8;                     // >4 so the steady-state period separates from fill
static const int  MEM_DW   = 64;
static const int  BPW      = MEM_DW / 8;            // bytes per word = 8
static const int  LW       = MEM_DW / 32;           // 32-bit elems per word = 2
static const int  NW       = (N + LW - 1) / LW;     // words per array = 128
static const int  MEM_NW   = 8192;
static const long MAX_CYCLES = 2000000L;

int main() {
    // 1) Participants.  Memory seeds itself from vectors/mem_in (the P + X arena); the command driver
    //    plays vectors/cmd; the arena dumps to vectors/out for inspection.
    FlatMemory      mem(MEM_NW, BPW);
    XsiSim          sim(ports::DESIGN_DLL, "interleaver_canon_bfm.wdb");
    sim.pin_low(ports::ZERO_PORTS, ports::ZERO_PORTS_N);

    AxisMaster      s_cmd (sim.dut(), ports::s_cmd, {});
    AxisSlave       s_done(sim.dut(), ports::s_done);
    AxiMmReadSlave  gmem0 (sim.dut(), ports::m_in,  mem);
    AxiMmWriteSlave gmem1 (sim.dut(), ports::m_out, mem);

    mem.load_segs   = { { (size_t)0, 0, "vectors/mem_in" } };
    mem.dump_segs   = { { (size_t)0, (size_t)MEM_NW, "vectors/out" } };
    s_cmd.in_bundle = "vectors/cmd";

    // Declaration order = the old sample/update/drive order (memory first, no-op on the cycle phases).
    std::vector<XsiSimObj*> parts = { &mem, &s_cmd, &s_done, &gmem0, &gmem1 };

    // s_done carries the 2-word InterleaverCmd token per job -> one job done per 2 beats.
    int  done_count = 0;
    long done_cyc[NJ];

    for (auto* p : parts) p->pre_sim();          // seed memory + load the commands before reset
    sim.reset([&]{ for (auto* p : parts) p->drive(); });

    // 2) Cycle loop.
    long cyc = 0, drain = -1; bool timed_out = false;
    for (;;) {
        if (drain >= 0 && (cyc - drain) >= 512) break;
        if (done_count >= NJ && drain < 0) drain = cyc;
        if (cyc >= MAX_CYCLES) { timed_out = true; break; }
        ++cyc;

        sim.clock_low();
        for (auto* p : parts) p->sample();
        sim.clock_high();
        for (auto* p : parts) p->update();
        while (done_count < (int)(s_done.count() / 2) && done_count < NJ) done_cyc[done_count++] = cyc;
        for (auto* p : parts) p->drive();
    }

    for (auto* p : parts) p->post_sim();          // dump the arena to vectors/out

    if (timed_out) {
        std::fprintf(stderr, "FAILED test: TIMEOUT after %ld cycles, done=%d/%d (cmd_widx=%d/%d "
                     "g0=%d g1=%d)\n", cyc, done_count, NJ, s_cmd.sent(), s_cmd.total(),
                     gmem0.state(), gmem1.state());
        sim.close();
        return 1;
    }

    // 3) Golden: the expected arena (Y regions = X[P]) is a bundle written from the same scenario, so
    //    the pattern is stated once, in Python.  Compare the written Y region of each job word-wise.
    const std::vector<uint64_t> golden = BurstBundle::read_words("vectors/golden");
    int fails = 0;
    for (int j = 0; j < NJ; ++j) {
        int yj = j * 3 * NW + 2 * NW;
        for (int k = 0; k < NW; ++k) {
            if (mem[yj + k] != golden[yj + k]) {
                if (fails < 8) std::fprintf(stderr, "  job %d word %d: got 0x%016llx exp 0x%016llx\n",
                                            j, k, (unsigned long long)mem[yj + k],
                                            (unsigned long long)golden[yj + k]);
                ++fails;
            }
        }
    }

    // `cycles` is time-to-last-job-done, NOT the loop count (which includes a fixed drain tail).
    long latency = (drain >= 0) ? drain : cyc;
    std::printf("interleaver_canon XSI BFM: n=%d nj=%d cycles=%ld (tail=%ld) done=%d/%d\n",
                N, NJ, latency, cyc - latency, done_count, NJ);
    std::printf("  per-job done cycles (period in parens):\n   ");
    for (int j = 0; j < done_count; ++j)
        std::printf(" j%d=%ld(%s%ld)", j, done_cyc[j],
                    j ? "+" : "fill=", j ? done_cyc[j] - done_cyc[j-1] : done_cyc[j]);
    std::printf("\n");
    sim.close();
    if (fails) { std::printf("FAILED test: %d mismatched words\n", fails); return 1; }
    std::printf("PASSED test\n");
    return 0;
}
