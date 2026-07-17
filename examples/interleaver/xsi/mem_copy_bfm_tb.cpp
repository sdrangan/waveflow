// mem_copy_bfm_tb.cpp — XSI cycle-based BFM that RTL-verifies the generated free-running MemCopy
// composite (gen/mem_copy.cpp) in xsim on Windows.  The Phase-2 (Gate 2) harness: it proves the
// GENERATED multi-hls::task top (Sequencer -> MemRStream -> MemWStream, wired by internal FIFOs)
// memcpy's a word run bit-exact through real RTL.  Since 8a182e8 the Sequencer's task body is itself
// generated from its run_iter, so this TB is also what proves that lowering functionally.
//
// The kernel is ap_ctrl_none (free-running); Vitis C/RTL cosim refuses it, so we drive the elaborated
// RTL directly through XSI.  Its four boundary interfaces are modelled by the reusable BFM
// (xsi_bfm.h) rather than hand-rolled here:
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
static const int  BPW      = vec::MEM_DW / 8;      // bytes per word = 8

// How long to run.  Fixed, and comfortably past the ~2835 completion — the run costs a few
// milliseconds either way, and undersizing it fails loudly (the done-count check below) rather than
// quietly.  This is the number the LT timing model would eventually supply: `predicted x margin`.
static const long N_CYCLES = 3400L;

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

    // 3) Cycle loop — FIXED N, no early termination, no design knowledge.
    //
    // This is the shape a generated testbench can emit, and the reason is the point: nothing here
    // blocks, so there is no sequencing to schedule and the DUT's pipelining survives BY
    // CONSTRUCTION.  The source keeps offering commands the moment they are accepted, so the
    // Sequencer is already on job j+1 while MemWStream is still storing job j — which is the whole
    // ~1.9x.  A loop that instead did "drive job j, await job j, repeat" would produce a correct
    // memcpy, a bit-exact golden, and ~334 cyc/job instead of ~177.
    //
    // Early termination is a later add-on (a polled predicate); it is an optimisation of wall-clock,
    // not of correctness.  N just has to be comfortably past completion — undersize it and the
    // done-count check below fails loudly rather than passing quietly.
    for (long c = 0; c < N_CYCLES; ++c) {
        sim.clock_low();
        sample();
        sim.clock_high();
        update();
        drive();
    }

    // Completion time comes from the SINK, which timestamped each word — never from the loop, whose
    // bound is a testbench constant.  Keeping those apart is exactly what the old `cycles=` bug got
    // wrong (it reported loop count + drain tail as the design's latency).
    const int  done_count = (int)(s_done.count() / vec::DONE_WORDS);
    const long done_cycle = s_done.cycle_of_word((size_t)vec::NUM_CMDS * vec::DONE_WORDS);

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
    // `cycles` is time-to-last-job-done, reported by the sink — NOT the loop bound, which is a
    // testbench constant.  This is where the pipelining shows up: ~177 cyc/job across 16 jobs
    // against ~176 for ONE write on its own (mem_w), i.e. the reads hide entirely behind the writes.
    std::printf("mem_copy XSI BFM: jobs=%d N=%d done=%d w_count=%d cycles=%ld (ran=%ld) job_fails=%d\n",
                vec::NUM_CMDS, vec::N, done_count, gmem1.w_count(), done_cycle, N_CYCLES, job_fails);
    sim.close();
    if (fails || done_count != vec::NUM_CMDS || job_fails) {
        std::printf("FAILED test: %d mismatches, done=%d/%d, %d job-index echo mismatches\n",
                    fails, done_count, vec::NUM_CMDS, job_fails);
        return 1;
    }
    std::printf("PASSED test\n");
    return 0;
}
