// rf_shot_tx_unified_loop.cpp -- HAND-WRITTEN.  Scenario TWO: an INFINITE shot, preempted.
//
// WHY A SECOND MAIN AND NOT A SECOND TESTBENCH.
//
// The graph is identical -- same DUT, same converter, same driver and sinks -- and only the input
// bundle differs.  A second testbench *graph* would be a second model of one design, which is the
// trap this arc has paid for more than once; a second main reassigns three bundle names on the
// generated harness and changes nothing else.  Everything the harness knows still comes from the
// component graph.
//
// WHY THE TWO SCENARIOS CANNOT BE ONE STREAM.
//
// A file-driven driver pushes every frame back to back and never reads a verdict.  A FINITE shot is
// therefore BUSY for every frame behind it -- that is not a limitation of the testbench, it is what
// SHOT_BUSY *is*.  So a stream that opens with a finite shot can never demonstrate a second load
// landing, and a stream that opens with an infinite one can never demonstrate a refusal.  One
// scenario per opcode is the minimum, and it is also the whole point of the merge: the SAME RTL
// answers both streams, and the only difference is which opcode arrived first.
//
// WHAT THIS ONE PROVES, that the finite scenario cannot:
//
//   * a load arriving mid-play is ACCEPTED and PREEMPTS -- so the waveform on the wire CHANGES.  The
//     bundle is read back in Python and the switch is checked there; the counter that matters here
//     is DAC_UNDERRUN, because a preemption is a handover and a handover is where a converter
//     starves if the ordering is wrong.
//   * SHOT_SHORT -- a TLAST before the shot is full -- and then SILENCE.  The loop-only predecessor
//     could not go quiet and played the padded result; this design has a way to stop, so the
//     stricter rule wins and the tail of this run is filler.  DAC_UNDERRUN must STILL be zero: quiet
//     is a VALUE, not an absence.
//
// Regenerate the harness, not this file.
#include <cstdio>

#include "rf_shot_tx_unified_tb_harness.h"

int main() {
    rf_shot_tx_unified_tb::Harness h("rf_shot_tx_unified_loop.wdb");
    // Reassigned BEFORE run(), because the models load and dump in pre_sim / post_sim -- the same
    // lifecycle phases the generated harness sets these in, so this is a different value for the
    // same knob rather than a different mechanism.
    h.s_in.in_bundle = "vectors/cmd_loop";
    h.resp_out.out_bundle = "vectors/resp_loop";
    h.xsi_tb_dac_if_rx.out_bundle = "vectors/rf_out_loop";
    h.run(1400);

    std::printf("DAC_WORDS_RECV=%llu\n", (unsigned long long)h.samp_out.words_recv);
    std::printf("DAC_UNDERRUN=%llu\n",   (unsigned long long)h.samp_out.underrun);
    std::printf("DAC_LAST_UNDERRUN_CYCLE=%llu\n",
                (unsigned long long)h.samp_out.last_underrun_cycle);
    std::printf("DAC_BLOCKS_OUT=%llu\n", (unsigned long long)h.samp_out.blocks_out);
    std::printf("DAC_BLOCKS_ZERO_FILLED=%llu\n",
                (unsigned long long)h.samp_out.blocks_zero_filled);
    std::printf("DAC_LAST_ZERO_FILL_IDX=%llu\n",
                (unsigned long long)h.samp_out.last_zero_fill_idx);
    std::printf("CMD_SENT=%d\n",  h.s_in.sent());
    std::printf("CMD_TOTAL=%d\n", h.s_in.total());
    std::printf("RESP_WORDS=%zu\n", h.resp_out.count());
    std::printf("RESP_LAST_CYCLE=%ld\n", h.resp_out.cycle_of_word(h.resp_out.count()));
    std::printf("RF_BLOCKS_IN=%llu\n", (unsigned long long)h.xsi_tb_dac_if_rx.blocks_in);

    h.close();
    return 0;
}
