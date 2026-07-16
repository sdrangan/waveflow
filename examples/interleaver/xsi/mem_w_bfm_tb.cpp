// mem_w_bfm_tb.cpp — XSI cycle-based AXI-MM + AXIS BFM that RTL-verifies the generated free-running
// MemWStream kernel (gen/mem_w_stream.cpp) in xsim on Windows.  Mirror of mem_r_bfm_tb.cpp.
//
// Gate: MemWStream drains N known words off s_in and pure-writes them to mem[BASE_W .. BASE_W+N);
// the backing memory region equals the input bit-exact.  TB drives s_cmd (master), s_in (master),
// and the gmem0 AXI write slave (AWREADY / WREADY / B*); the read side + control are pinned to 0.
// All three are the reusable models in bfm/xsi_bfm.h.
#include <cstdio>
#include <cstdint>
#include <string>
#include <vector>
#include "bfm/xsi_bfm.h"
#include "mem_w_stream_ports.h"      // GENERATED from the same TopSpec as the top's pragmas

using namespace wfbfm;
namespace ports = mem_w_stream_ports;

static const int  N        = 128;
static const int  MEM_DW   = 64;
static const int  BPW      = MEM_DW / 8;
static const int  MEM_NW   = 8192;
static const int  BASE_W   = 64;
static const long MAX_CYCLES = 2000000L;

static uint64_t known_word(int i) { return ((uint64_t)i * 40503ULL + 7ULL); }

int main() {
    FlatMemory mem(MEM_NW, BPW);

    // The command carries an ELEMENT/WORD coordinate (= the old word-aligned byte addr / BPW). The
    // offset=slave register stays pinned to 0, so the kernel's m_mem[word_index] drives
    // AWADDR = word_index*BPW — the AXI slave's awaddr/BPW->word decode is unchanged.
    const uint32_t word_index = (uint32_t)BASE_W;
    // MWCmd{addr, len, xfer_len, xfer_msg[8]} packs to 6 words at MEM_DW=64 (mirrors mem_r_bfm_tb.cpp).
    std::vector<uint64_t> cmd_words = {
        (uint64_t)word_index | ((uint64_t)(uint32_t)N << 32),
        0ULL,
        0ULL, 0ULL, 0ULL, 0ULL,
    };
    std::vector<uint64_t> in_words;
    for (int i = 0; i < N; ++i) in_words.push_back(known_word(i));

    // Open the design and model its three interfaces.
    XsiSim sim(ports::DESIGN_DLL, "mem_w_bfm.wdb");
    sim.pin_low(ports::ZERO_PORTS, ports::ZERO_PORTS_N);

    AxisMaster      s_cmd(sim.dut(), ports::s_cmd, cmd_words);
    AxisMaster      s_in (sim.dut(), ports::s_in,  in_words);
    AxiMmWriteSlave gmem0(sim.dut(), ports::m_mem, mem);

    auto sample = [&]{ s_cmd.sample(); s_in.sample(); gmem0.sample(); };
    auto update = [&]{ s_cmd.update(); s_in.update(); gmem0.update(); };
    auto drive  = [&]{ s_cmd.drive();  s_in.drive();  gmem0.drive();  };

    sim.reset(drive);

    long cyc = 0, drain = -1; bool timed_out = false;
    for (;;) {
        if (drain >= 0 && (cyc - drain) >= 256) break;
        // Drain only once the write is ACKNOWLEDGED, not merely sent: w_count alone would let the
        // run stop before the last burst's B response.
        if (gmem0.w_count() >= N && gmem0.saw_b() && drain < 0) drain = cyc;
        if (cyc >= MAX_CYCLES) { timed_out = true; break; }
        ++cyc;

        sim.clock_low();
        sample();
        sim.clock_high();
        update();
        drive();
    }

    if (timed_out) {
        std::fprintf(stderr, "FAILED test: TIMEOUT cyc=%ld w_count=%d/%d in_idx=%d cmd_widx=%d/%d g_state=%d\n",
                     cyc, gmem0.w_count(), N, s_in.sent(), s_cmd.sent(), s_cmd.total(), gmem0.state());
        sim.close();
        return 1;
    }

    int fails = 0;
    for (int i = 0; i < N; ++i) {
        uint64_t exp = known_word(i), got = mem[BASE_W + i];
        if (got != exp) {
            if (fails < 8) std::fprintf(stderr, "  word %d: got 0x%016llx exp 0x%016llx\n",
                                        i, (unsigned long long)got, (unsigned long long)exp);
            ++fails;
        }
    }
    std::printf("mem_w_stream XSI BFM: N=%d w_count=%d cycles=%ld\n", N, gmem0.w_count(), cyc);
    sim.close();
    if (fails) { std::printf("FAILED test: %d mismatched words\n", fails); return 1; }
    std::printf("PASSED test\n");
    return 0;
}
