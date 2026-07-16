// interleaver_canon_bfm_tb.cpp — XSI BFM for the CANONICAL six-stage interleaver
// (gen/interleaver_canon.cpp): cmd_rx -> il_mem_r -> il_load -> il_compute -> il_store -> il_mem_w,
// with one InterleaverCmd token per job forwarded through every stage (sob3's per-job pacing).  The
// definitive test that token forwarding breaks the done==#tasks+1 deadlock the mix (7/8) and P-SOB
// (6/8) variants hit.  Same boundary/memory model as the other interleaver BFMs; the ONLY difference
// is s_done carries the 2-word InterleaverCmd token per job (one job-done per 2 s_done beats).
//
// Adapted from sandbox/il_1d/xsi_task/il_bfm_tb.cpp.  Two differences from that byte-addressed sob3
// harness: (1) the command carries element/word OFFSETS (the word_index convention), and (2) the
// generated s_cmd is a 64-bit AXIS carrying the 128-bit InterleaverCmd as TWO beats.  Everything else
// (one flat memory behind gmem0 read + gmem1 write, the AXI-MM slave FSMs, per-job done-cycle
// recording) is identical.  The kernel's m_mem[word_index] drives ARADDR = word_index*BPW, so the
// slave's araddr/BPW -> word decode is unchanged.  ap_ctrl_none: driven directly through XSI (Vitis
// cosim is unreliable).  Per-job done cycles print the steady-state period (slope) = throughput.
#include <cstdio>
#include <cstring>
#include <cstdint>
#include <string>
#include <vector>
#include "bfm/xsi_bfm.h"
#include "interleaver_canon_ports.h"      // GENERATED from the same TopSpec as the top's pragmas

using namespace wfbfm;
namespace ports = interleaver_canon_ports;

// VERDICT: PASS — forwarding a per-job token through every stage BREAKS the done==#tasks+1 law.
// nj=8 -> 8/8 (mix hung at 7/8, P-SOB at 6/8) and nj=16 -> 16/16 (truly unbounded, not a higher
// tasks+1), bit-exact Y[i]=X[P[i]] with commit-timed done tokens. Steady-state 414 cyc/job (above the
// load-bound ~296: the strict per-job token pacing trades some overlap for the robustness).
static const int  N        = 256;
static const int  NJ       = 8;    // >4 so the steady-state period (slope) separates from fill latency
static const int  MEM_DW   = 64;
static const int  BPW      = MEM_DW / 8;             // bytes per word = 8
static const int  LW       = MEM_DW / 32;            // 32-bit elems per word = 2
static const int  NW       = (N + LW - 1) / LW;      // words per array = 128
static const int  MEM_NW   = 8192;
static const long MAX_CYCLES = 2000000L;

static uint32_t fbits(float f) { uint32_t u; std::memcpy(&u, &f, 4); return u; }
static int   Pidx(int i)        { return ((i * 13 + 5) % N); }
static float Xval(int j, int k) { return (float)((float)k * 0.5f - 3.0f + (float)j); }

int main() {
    // 1) Shared memory + commands. Layout per job: P then X then Y, NW words each, base = j*3*NW.
    FlatMemory mem(MEM_NW, BPW);
    int yw[NJ];
    std::vector<uint64_t> cmd_flat;                 // 2 words per InterleaverCmd (word offsets)
    for (int j = 0; j < NJ; ++j) {
        int base = j * 3 * NW;
        int pw = base, xw = base + NW, yj = base + 2 * NW;
        yw[j] = yj;
        for (int i = 0; i < N; ++i) {
            uint32_t pbits = (uint32_t)Pidx(i);
            uint32_t xbits = fbits(Xval(j, i));
            int      lane  = i % LW;
            uint64_t shift = (uint64_t)(lane * 32);
            uint64_t keep  = ~((uint64_t)0xFFFFFFFFull << shift);
            mem[pw + i/LW] = (mem[pw + i/LW] & keep) | ((uint64_t)pbits << shift);
            mem[xw + i/LW] = (mem[xw + i/LW] & keep) | ((uint64_t)xbits << shift);
        }
        // InterleaverCmd{p_off,x_off,y_off,n} -> word0 = p_off|(x_off<<32), word1 = y_off|(n<<32).
        cmd_flat.push_back((uint64_t)(uint32_t)pw | ((uint64_t)(uint32_t)xw << 32));
        cmd_flat.push_back((uint64_t)(uint32_t)yj | ((uint64_t)(uint32_t)N << 32));
    }
    // 2) Open the design and model its four interfaces.
    XsiSim sim(ports::DESIGN_DLL, "interleaver_canon_bfm.wdb");
    sim.pin_low(ports::ZERO_PORTS, ports::ZERO_PORTS_N);

    AxisMaster      s_cmd (sim.dut(), ports::s_cmd, cmd_flat);
    AxisSlave       s_done(sim.dut(), ports::s_done);
    AxiMmReadSlave  gmem0 (sim.dut(), ports::m_in, mem);
    AxiMmWriteSlave gmem1 (sim.dut(), ports::m_out, mem);

    auto sample = [&]{ s_cmd.sample(); s_done.sample(); gmem0.sample(); gmem1.sample(); };
    auto update = [&]{ s_cmd.update(); s_done.update(); gmem0.update(); gmem1.update(); };
    auto drive  = [&]{ s_cmd.drive();  s_done.drive();  gmem0.drive();  gmem1.drive();  };

    // s_done carries the 2-word InterleaverCmd token per job -> one job done per 2 beats.
    int  done_count = 0;
    long done_cyc[NJ];

    sim.reset(drive);

    // 5) Cycle loop.
    long cyc = 0, drain = -1; bool timed_out = false;
    for (;;) {
        if (drain >= 0 && (cyc - drain) >= 512) break;
        if (done_count >= NJ && drain < 0) drain = cyc;
        if (cyc >= MAX_CYCLES) { timed_out = true; break; }
        ++cyc;

        sim.clock_low();
        sample();
        sim.clock_high();
        update();
        // Record the cycle each job completes (the period print = throughput).  Derived from the
        // sink's beat count, so it lands on the same cycle the inline check used to.
        while (done_count < (int)(s_done.count() / 2) && done_count < NJ) done_cyc[done_count++] = cyc;
        drive();
    }

    if (timed_out) {
        std::fprintf(stderr, "FAILED test: TIMEOUT after %ld cycles, done=%d/%d (cmd_widx=%d/%d "
                     "g0=%d g1=%d)\n  done cycles:", cyc, done_count, NJ, s_cmd.sent(), s_cmd.total(),
                     gmem0.state(), gmem1.state());
        for (int j = 0; j < done_count; ++j)
            std::fprintf(stderr, " j%d=%ld(%s%ld)", j, done_cyc[j],
                         j ? "+" : "fill=", j ? done_cyc[j] - done_cyc[j-1] : done_cyc[j]);
        std::fprintf(stderr, "\n");
        sim.close();
        return 1;
    }

    // 6) Golden check: every Y[i] == X[P[i]] bit-exact, unpacked from mem.
    int fails = 0;
    for (int j = 0; j < NJ; ++j) {
        for (int i = 0; i < N; ++i) {
            uint64_t w = mem[yw[j] + i/LW];
            uint32_t got = (uint32_t)((w >> ((i % LW) * 32)) & 0xFFFFFFFFull);
            uint32_t exp = fbits(Xval(j, Pidx(i)));
            if (got != exp) {
                if (fails < 8) std::fprintf(stderr, "  job %d elem %d: got 0x%08x exp 0x%08x\n",
                                            j, i, got, exp);
                ++fails;
            }
        }
    }

    long latency = (drain >= 0) ? drain : cyc;
    std::printf("interleaver_canon XSI BFM: n=%d nj=%d cycles=%ld done=%d/%d\n", N, NJ, latency, done_count, NJ);
    std::printf("  per-job done cycles (period in parens):\n   ");
    for (int j = 0; j < done_count; ++j)
        std::printf(" j%d=%ld(%s%ld)", j, done_cyc[j],
                    j ? "+" : "fill=", j ? done_cyc[j] - done_cyc[j-1] : done_cyc[j]);
    std::printf("\n  steady-state period ~= n/job (2*(n/LW) read words) for MEM_DW=64; sob3 ~= 295\n");
    sim.close();
    if (fails) { std::printf("FAILED test: %d mismatched elements\n", fails); return 1; }
    std::printf("PASSED test\n");
    return 0;
}
