// mem_copy_bfm_tb.cpp — XSI cycle-based BFM that RTL-verifies the generated free-running MemCopy
// composite (gen/mem_copy.cpp) in xsim on Windows.  It proves the GENERATED multi-hls::task top
// (Sequencer -> MemRStream -> MemWStream, wired by internal FIFOs) memcpy's a word run bit-exact
// through real RTL — and, since 8a182e8, that the Sequencer's task body generated from its run_iter
// is functionally identical to the hand-written one it replaced.
//
// The kernel is ap_ctrl_none (free-running); Vitis C/RTL cosim refuses it, so we drive the elaborated
// RTL directly through XSI.  Almost nothing below is about that, because almost none of it is
// hand-written any more:
//
//   * mem_copy_tb_harness.h  — GENERATED from the MemCopyTB graph: which models drive which RTL
//                              ports, the per-cycle phases, and the fixed-N run loop.
//   * mem_copy_vectors.h     — GENERATED from the same graph: the scenario, and the driver's own
//                              commands packed by CopyCmd.serialize().
//   * mem_copy_ports.h       — GENERATED from the TopSpec that emits the top's interface pragmas.
//
// What is left here is the TEST: what to put in memory, and what to check afterwards.  That is the
// part a component graph cannot know.
#include <cstdio>
#include <cstdint>
#include <vector>
#include "mem_copy_tb_harness.h"   // GENERATED: the participants, phases and run loop
#include "mem_copy_vectors.h"      // GENERATED: the scenario + CopyCmd.serialize()'s own output

namespace vec = mem_copy_vectors;

// How long to run.  Fixed, comfortably past the ~2835 completion; undersizing it fails loudly (the
// done-count check below), never quietly.  This is the number the LT timing model would supply.
static const long N_CYCLES = 3400L;

// The memory pattern — the one thing still stated both here and in MemCopyTB's `expected`.
// Emitting it as data would close that; see plans/xsi_tb_codegen.md Stage 4.
static uint64_t known_word(int job, int i) {
    return ((uint64_t)i * 2654435761ULL + 12345ULL + (uint64_t)job * 7919ULL);
}

int main() {
    // 1) The harness: everything derived from the graph.  The command words are a burst bundle
    //    (vectors/s_cmd) the harness loads in pre_sim -- no longer baked in or passed here.
    mem_copy_tb::Harness h("mem_copy_bfm.wdb");

    // 2) The test's stimulus: a known pattern in each source region of the shared arena.
    for (int j = 0; j < vec::NUM_CMDS; ++j)
        for (int i = 0; i < vec::N; ++i) h.mem[vec::SRC_W[j] + i] = known_word(j, i);

    h.run(N_CYCLES);

    // 3) Completion time comes from the SINK, which timestamped each word — never from the loop,
    // whose bound is a testbench constant.
    const int  done_count = (int)(h.s_done.count() / vec::DONE_WORDS);
    const long done_cycle = h.s_done.cycle_of_word((size_t)vec::NUM_CMDS * vec::DONE_WORDS);

    // 4) Golden: every destination region equals its source region (a memcpy), and each job's
    // MemComplete echoes back xfer_msg[0] = the tx_id the host set on the command (here tx_id == j).
    int fails = 0, job_fails = 0;
    for (int j = 0; j < vec::NUM_CMDS; ++j) {
        for (int i = 0; i < vec::N; ++i) {
            uint64_t exp = known_word(j, i), got = h.mem[vec::DST_W[j] + i];
            if (got != exp) {
                if (fails < 8) std::fprintf(stderr, "  job %d word %d: got 0x%016llx exp 0x%016llx\n",
                                            j, i, (unsigned long long)got, (unsigned long long)exp);
                ++fails;
            }
        }
    }
    for (int j = 0; j < done_count; ++j) {
        uint32_t got_job = (uint32_t)h.s_done.words()[j * vec::DONE_WORDS + 1];  // low 32b of xfer_msg[0]
        if (got_job != (uint32_t)j) {
            if (job_fails < 8) std::fprintf(stderr, "  job %u: xfer_msg echo got=%u exp=%u\n",
                                            (unsigned)j, got_job, (unsigned)j);
            ++job_fails;
        }
    }

    // `cycles` is time-to-last-job-done.  This is where the pipelining shows up: ~177 cyc/job across
    // 16 jobs against ~176 for ONE write alone (mem_w), i.e. the reads hide entirely behind the
    // writes — max(read, write), not read + write.
    std::printf("mem_copy XSI BFM: jobs=%d N=%d done=%d w_count=%d cycles=%ld (ran=%ld) job_fails=%d\n",
                vec::NUM_CMDS, vec::N, done_count, h.m_out.w_count(), done_cycle, N_CYCLES, job_fails);
    h.close();
    if (fails || done_count != vec::NUM_CMDS || job_fails) {
        std::printf("FAILED test: %d mismatches, done=%d/%d, %d job-index echo mismatches\n",
                    fails, done_count, vec::NUM_CMDS, job_fails);
        return 1;
    }
    std::printf("PASSED test\n");
    return 0;
}
