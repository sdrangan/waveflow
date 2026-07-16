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
#include "xsi_bfm.h"
#include "mem_copy_ports.h"      // GENERATED from the same TopSpec that emits the top's pragmas
#include "mem_copy_vectors.h"    // GENERATED: the scenario + CopyCmd.serialize()'s own output

using namespace wfbfm;
namespace ports = mem_copy_ports;
namespace vec = mem_copy_vectors;

// The scenario (N / NUM_CMDS / SRC_W / DST_W), the s_done framing (DONE_WORDS =
// MemComplete.nwords_per_inst), and the command words all come from the generated header.  In
// particular this TB no longer packs CopyCmd itself: CMD_WORDS is what CopyCmd.serialize() actually
// produced, so there is no second implementation of the packing rule to drift from the schema.
static const int  BPW        = vec::MEM_DW / 8;      // bytes per word = 8
static const long MAX_CYCLES = 2000000L;

// The memory pattern.  Still stated here AND in mem_copy_sim.py's run_copy() — see the note at the
// bottom of gen_xsi_vectors(); unifying it means emitting the whole arena image as data.
static uint64_t known_word(int job, int i) {
    return ((uint64_t)i * 2654435761ULL + 12345ULL + (uint64_t)job * 7919ULL);
}

int main() {
    // 1) Shared backing memory: a known pattern in each source region.
    FlatMemory mem(vec::MEM_NW, BPW);
    for (int j = 0; j < vec::NUM_CMDS; ++j)
        for (int i = 0; i < vec::N; ++i) mem[vec::SRC_W[j] + i] = known_word(j, i);

    // CopyCmd stream words, straight from the schema's serialize() via the generated header.
    std::vector<uint64_t> cmd_words(vec::CMD_WORDS,
                                    vec::CMD_WORDS + vec::NUM_CMDS * vec::CMD_WORDS_PER_CMD);

    // 2) Open the design and model its four interfaces.  Every port name comes from the generated
    // binding, which is derived from the same TopSpec that emitted the top's interface pragmas —
    // so this TB cannot name a port the kernel does not have.
    XsiSim sim(ports::DESIGN_DLL, "mem_copy_bfm.wdb");
    sim.pin_low(ports::ZERO_PORTS, ports::ZERO_PORTS_N);

    AxisMaster      s_cmd (sim.dut(), ports::s_cmd,  cmd_words);
    AxisSlave       s_done(sim.dut(), ports::s_done);
    AxiMmReadSlave  gmem0 (sim.dut(), ports::m_in,  mem);
    AxiMmWriteSlave gmem1 (sim.dut(), ports::m_out, mem);

    auto sample = [&]{ s_cmd.sample(); s_done.sample(); gmem0.sample(); gmem1.sample(); };
    auto update = [&]{ s_cmd.update(); s_done.update(); gmem0.update(); gmem1.update(); };
    auto drive  = [&]{ s_cmd.drive();  s_done.drive();  gmem0.drive();  gmem1.drive();  };

    sim.reset(drive);

    // 3) Cycle loop.  Drain a fixed tail after the last job so any trailing bus activity settles.
    long cyc = 0, drain = -1; bool timed_out = false;
    for (;;) {
        int done_count = (int)(s_done.count() / vec::DONE_WORDS);
        if (drain >= 0 && (cyc - drain) >= 512) break;
        if (done_count >= vec::NUM_CMDS && drain < 0) drain = cyc;
        if (cyc >= MAX_CYCLES) { timed_out = true; break; }
        ++cyc;

        sim.clock_low();
        sample();
        sim.clock_high();
        update();
        drive();
    }

    const int done_count = (int)(s_done.count() / vec::DONE_WORDS);
    if (timed_out) {
        std::fprintf(stderr, "FAILED test: TIMEOUT cyc=%ld done=%d/%d w_count=%d cmd_widx=%d/%d "
                     "g_rstate=%d g_wstate=%d\n",
                     cyc, done_count, vec::NUM_CMDS, gmem1.w_count(), s_cmd.sent(), s_cmd.total(),
                     gmem0.state(), gmem1.state());
        sim.close();
        return 1;
    }

    // 4) Golden check: every destination region equals its source region (a memcpy), and each job's
    // MemComplete echoes back the xfer_msg[0] job index the sequencer stamped.
    int fails = 0, job_fails = 0;
    for (int j = 0; j < vec::NUM_CMDS; ++j) {
        for (int i = 0; i < vec::N; ++i) {
            uint64_t exp = known_word(j, i), got = mem[vec::DST_W[j] + i];
            if (got != exp) {
                if (fails < 8) std::fprintf(stderr, "  job %d word %d: got 0x%016llx exp 0x%016llx\n",
                                            j, i, (unsigned long long)got, (unsigned long long)exp);
                ++fails;
            }
        }
    }
    for (int j = 0; j < done_count; ++j) {
        uint32_t got_job = (uint32_t)s_done.words()[j * vec::DONE_WORDS + 1];   // low 32b of xfer_msg[0]
        if (got_job != (uint32_t)j) {
            if (job_fails < 8) std::fprintf(stderr, "  job %u: xfer_msg echo got=%u exp=%u\n",
                                            (unsigned)j, got_job, (unsigned)j);
            ++job_fails;
        }
    }
    std::printf("mem_copy XSI BFM: jobs=%d N=%d done=%d w_count=%d cycles=%ld job_fails=%d\n",
                vec::NUM_CMDS, vec::N, done_count, gmem1.w_count(), cyc, job_fails);
    sim.close();
    if (fails || done_count != vec::NUM_CMDS || job_fails) {
        std::printf("FAILED test: %d mismatches, done=%d/%d, %d job-index echo mismatches\n",
                    fails, done_count, vec::NUM_CMDS, job_fails);
        return 1;
    }
    std::printf("PASSED test\n");
    return 0;
}
