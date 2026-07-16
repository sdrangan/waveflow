// mem_copy_bfm_tb.cpp — XSI cycle-based BFM that RTL-verifies the generated free-running MemCopy
// composite (gen/mem_copy.cpp) in xsim on Windows.  The Phase-2 (Gate 2) harness: it proves the
// GENERATED multi-hls::task top (Sequencer -> MemRStream -> MemWStream, wired by internal FIFOs)
// memcpy's a word run bit-exact through real RTL.  Since 8a182e8 the Sequencer's task body is itself
// generated from its run_iter, so this TB is also what proves that lowering functionally.
//
// The kernel is ap_ctrl_none (free-running); Vitis C/RTL cosim refuses it, so we drive the elaborated
// RTL directly through XSI.  Its four boundary interfaces are modelled by the reusable BFM
// (bfm/xsi_bfm.h) rather than hand-rolled here:
//   * s_cmd  (AXIS slave  on the kernel; TB master) — streams CopyCmd{src_off,dst_off,n_words}, 2
//             words each; back-to-back commands exercise the hls::task re-fire across jobs.
//   * s_done (AXIS master on the kernel; TB slave)  — one MemComplete per job.
//   * gmem0  (AXI-MM read)  / gmem1 (AXI-MM write)  — both served from ONE flat arena (the
//     offset=slave registers are pinned to 0), so the copy reads the source region and writes the
//     destination region of the same buffer.
#include <cstdio>
#include <cstdint>
#include <string>
#include <vector>
#include "bfm/xsi_bfm.h"

using namespace wfbfm;

static const int  MEM_DW   = 64;
static const int  BPW      = MEM_DW / 8;             // bytes per word = 8
static const int  MEM_NW   = 8192;
static const int  N        = 128;                    // words per copy
static const int  NUM_CMDS = 16;                     // back-to-back jobs
// s_done streams one MemComplete{len,xfer_len,xfer_msg[8]} per job (word_bw=64):
// word0 = len|(xfer_len<<32), word1 = xfer_msg[0]|(xfer_msg[1]<<32), words2-4 = xfer_msg[2:8]
// (MemComplete.nwords_per_inst(64) == 5) — the sequencer stamps xfer_msg[0] with the job index.
static const int  DONE_WORDS = 5;
static const int  SRC_W[NUM_CMDS] = { 64, 192, 320, 448, 576, 704, 832, 960, 1088, 1216, 1344, 1472, 1600, 1728, 1856, 1984 };
static const int  DST_W[NUM_CMDS] = { 4096, 4224, 4352, 4480, 4608, 4736, 4864, 4992, 5120, 5248, 5376, 5504, 5632, 5760, 5888, 6016 };
static const long MAX_CYCLES = 2000000L;

static uint64_t known_word(int job, int i) {
    return ((uint64_t)i * 2654435761ULL + 12345ULL + (uint64_t)job * 7919ULL);
}

// Every TB-driven input this harness does not otherwise drive: the offset=slave registers (-> base
// 0), the unused write side of gmem0, the unused read side of gmem1, and a quiescent control slave.
static const char* const ZERO_PORTS[] = {
    "s_axi_control_AWVALID","s_axi_control_AWADDR","s_axi_control_WVALID","s_axi_control_WDATA",
    "s_axi_control_WSTRB","s_axi_control_ARVALID","s_axi_control_ARADDR","s_axi_control_RREADY",
    "s_axi_control_BREADY",
    "m_axi_gmem0_AWREADY","m_axi_gmem0_WREADY","m_axi_gmem0_BVALID","m_axi_gmem0_BRESP",
    "m_axi_gmem0_BID","m_axi_gmem0_BUSER","m_axi_gmem0_RRESP","m_axi_gmem0_RID","m_axi_gmem0_RUSER",
    "m_axi_gmem1_ARREADY","m_axi_gmem1_RVALID","m_axi_gmem1_RDATA","m_axi_gmem1_RLAST",
    "m_axi_gmem1_RRESP","m_axi_gmem1_RID","m_axi_gmem1_RUSER","m_axi_gmem1_BID","m_axi_gmem1_BUSER",
};

int main() {
    // 1) Shared backing memory: a known pattern in each source region.
    FlatMemory mem(MEM_NW, BPW);
    for (int j = 0; j < NUM_CMDS; ++j)
        for (int i = 0; i < N; ++i) mem[SRC_W[j] + i] = known_word(j, i);

    // CopyCmd stream words (2 per command): word0 = src|(dst<<32), word1 = n_words.
    std::vector<uint64_t> cmd_words;
    for (int j = 0; j < NUM_CMDS; ++j) {
        cmd_words.push_back((uint64_t)(uint32_t)SRC_W[j] | ((uint64_t)(uint32_t)DST_W[j] << 32));
        cmd_words.push_back((uint64_t)(uint32_t)N);
    }

    // 2) Open the design and model its four interfaces.
    XsiSim sim("xsim.dir/mem_copy/xsimk.dll", "mem_copy_bfm.wdb");
    sim.pin_low(ZERO_PORTS, sizeof(ZERO_PORTS)/sizeof(ZERO_PORTS[0]));

    AxisMaster      s_cmd (sim.dut(), "s_cmd", cmd_words);
    AxisSlave       s_done(sim.dut(), "s_done");
    AxiMmReadSlave  gmem0 (sim.dut(), "m_axi_gmem0", mem);
    AxiMmWriteSlave gmem1 (sim.dut(), "m_axi_gmem1", mem);

    auto sample = [&]{ s_cmd.sample(); s_done.sample(); gmem0.sample(); gmem1.sample(); };
    auto update = [&]{ s_cmd.update(); s_done.update(); gmem0.update(); gmem1.update(); };
    auto drive  = [&]{ s_cmd.drive();  s_done.drive();  gmem0.drive();  gmem1.drive();  };

    sim.reset(drive);

    // 3) Cycle loop.  Drain a fixed tail after the last job so any trailing bus activity settles.
    long cyc = 0, drain = -1; bool timed_out = false;
    for (;;) {
        int done_count = (int)(s_done.count() / DONE_WORDS);
        if (drain >= 0 && (cyc - drain) >= 512) break;
        if (done_count >= NUM_CMDS && drain < 0) drain = cyc;
        if (cyc >= MAX_CYCLES) { timed_out = true; break; }
        ++cyc;

        sim.clock_low();
        sample();
        sim.clock_high();
        update();
        drive();
    }

    const int done_count = (int)(s_done.count() / DONE_WORDS);
    if (timed_out) {
        std::fprintf(stderr, "FAILED test: TIMEOUT cyc=%ld done=%d/%d w_count=%d cmd_widx=%d/%d "
                     "g_rstate=%d g_wstate=%d\n",
                     cyc, done_count, NUM_CMDS, gmem1.w_count(), s_cmd.sent(), s_cmd.total(),
                     gmem0.state(), gmem1.state());
        sim.close();
        return 1;
    }

    // 4) Golden check: every destination region equals its source region (a memcpy), and each job's
    // MemComplete echoes back the xfer_msg[0] job index the sequencer stamped.
    int fails = 0, job_fails = 0;
    for (int j = 0; j < NUM_CMDS; ++j) {
        for (int i = 0; i < N; ++i) {
            uint64_t exp = known_word(j, i), got = mem[DST_W[j] + i];
            if (got != exp) {
                if (fails < 8) std::fprintf(stderr, "  job %d word %d: got 0x%016llx exp 0x%016llx\n",
                                            j, i, (unsigned long long)got, (unsigned long long)exp);
                ++fails;
            }
        }
    }
    for (int j = 0; j < done_count; ++j) {
        uint32_t got_job = (uint32_t)s_done.words()[j * DONE_WORDS + 1];   // low 32b of xfer_msg[0]
        if (got_job != (uint32_t)j) {
            if (job_fails < 8) std::fprintf(stderr, "  job %u: xfer_msg echo got=%u exp=%u\n",
                                            (unsigned)j, got_job, (unsigned)j);
            ++job_fails;
        }
    }
    std::printf("mem_copy XSI BFM: jobs=%d N=%d done=%d w_count=%d cycles=%ld job_fails=%d\n",
                NUM_CMDS, N, done_count, gmem1.w_count(), cyc, job_fails);
    sim.close();
    if (fails || done_count != NUM_CMDS || job_fails) {
        std::printf("FAILED test: %d mismatches, done=%d/%d, %d job-index echo mismatches\n",
                    fails, done_count, NUM_CMDS, job_fails);
        return 1;
    }
    std::printf("PASSED test\n");
    return 0;
}
