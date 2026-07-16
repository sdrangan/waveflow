// mem_r_bfm_tb.cpp — XSI cycle-based AXI-MM + AXIS BFM that RTL-verifies the generated free-running
// MemRStream kernel (gen/mem_r_stream.cpp) in xsim on Windows.
//
// The kernel is ap_ctrl_none (free-running); Vitis C/RTL cosim refuses it, so we drive the elaborated
// RTL directly through XSI.  Its three interfaces are modelled by the reusable BFM (bfm/xsi_bfm.h):
// an AXIS master on s_cmd, an AXIS slave on m_out, and an AXI-MM read slave on gmem0 serving one flat
// arena.  Gate: MemRStream bursts mem[BASE_W .. BASE_W+N) onto m_out, and the collected stream equals
// the memory region bit-exact.
#include <cstdio>
#include <cstdint>
#include <string>
#include <vector>
#include "xsi_bfm.h"
#include "mem_r_stream_ports.h"      // GENERATED from the same TopSpec as the top's pragmas

using namespace wfbfm;
namespace ports = mem_r_stream_ports;

static const int  N        = 128;                    // words to burst
static const int  MEM_DW   = 64;
static const int  BPW      = MEM_DW / 8;             // bytes per word = 8
static const int  MEM_NW   = 8192;
static const int  BASE_W   = 64;                     // region base (word index)
static const long MAX_CYCLES = 2000000L;

static uint64_t known_word(int i) { return ((uint64_t)i * 2654435761ULL + 12345ULL); }

int main() {
    // 1) Backing memory: known pattern in [BASE_W, BASE_W+N).
    FlatMemory mem(MEM_NW, BPW);
    for (int i = 0; i < N; ++i) mem[BASE_W + i] = known_word(i);

    // MRCmd{addr, len, xfer_len, xfer_msg[8]} packs to 6 words at MEM_DW=64: word0 = addr|(len<<32),
    // word1 = xfer_len|(xfer_msg[0]<<32), words2-5 = xfer_msg[1:8] (2 per word) — all zero here (the
    // job-index cookie is only exercised by the mem_copy composite BFM).
    const uint32_t word_index = (uint32_t)BASE_W;
    std::vector<uint64_t> cmd_words = {
        (uint64_t)word_index | ((uint64_t)(uint32_t)N << 32),
        0ULL,
        0ULL, 0ULL, 0ULL, 0ULL,
    };

    // 2) Open the design and model its three interfaces.
    XsiSim sim(ports::DESIGN_DLL, "mem_r_bfm.wdb");
    sim.pin_low(ports::ZERO_PORTS, ports::ZERO_PORTS_N);

    AxisMaster     s_cmd(sim.dut(), ports::s_cmd, cmd_words);
    AxisSlave      m_out(sim.dut(), ports::m_out);
    AxiMmReadSlave gmem0(sim.dut(), ports::m_mem, mem);

    auto sample = [&]{ s_cmd.sample(); m_out.sample(); gmem0.sample(); };
    auto update = [&]{ s_cmd.update(); m_out.update(); gmem0.update(); };
    auto drive  = [&]{ s_cmd.drive();  m_out.drive();  gmem0.drive();  };

    sim.reset(drive);

    // 3) Cycle loop.
    long cyc = 0, drain = -1; bool timed_out = false;
    for (;;) {
        if (drain >= 0 && (cyc - drain) >= 256) break;
        if ((int)m_out.count() >= N && drain < 0) drain = cyc;
        if (cyc >= MAX_CYCLES) { timed_out = true; break; }
        ++cyc;

        sim.clock_low();
        sample();
        sim.clock_high();
        update();
        drive();
    }

    const std::vector<uint64_t>& got = m_out.words();
    if (timed_out) {
        std::fprintf(stderr, "FAILED test: TIMEOUT cyc=%ld got=%zu/%d cmd_widx=%d/%d g_state=%d\n",
                     cyc, got.size(), N, s_cmd.sent(), s_cmd.total(), gmem0.state());
        sim.close();
        return 1;
    }

    // 4) Golden check.
    int fails = 0;
    for (int i = 0; i < N; ++i) {
        uint64_t exp = known_word(i);
        if (i >= (int)got.size() || got[i] != exp) {
            if (fails < 8) std::fprintf(stderr, "  word %d: got 0x%016llx exp 0x%016llx\n",
                                        i, (unsigned long long)(i < (int)got.size() ? got[i] : 0),
                                        (unsigned long long)exp);
            ++fails;
        }
    }
    std::printf("mem_r_stream XSI BFM: N=%d collected=%zu cycles=%ld\n", N, got.size(), cyc);
    sim.close();
    if (fails || (int)got.size() != N) { std::printf("FAILED test: %d mismatches (got %zu)\n", fails, got.size()); return 1; }
    std::printf("PASSED test\n");
    return 0;
}
