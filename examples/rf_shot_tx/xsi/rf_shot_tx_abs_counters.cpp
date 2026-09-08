// rf_shot_tx_abs_counters.cpp -- HAND-WRITTEN.  The SAME finite scenario, on the SAME design,
// built with `absolute_index = 1` (plans/rf_shot_absolute.md).
//
// WHY A SECOND SNAPSHOT AND NOT A SECOND MAIN.
//
// `absolute_index` is a template argument, so the two settings are two pieces of RTL rather than one
// with a mode register in it -- and a gate that only ever elaborated one of them would be asserting
// the mode's behaviour against a simulator.  So this main drives `rf_shot_tx_abs_top`, its own
// xelab snapshot, from its own generated harness.  What it is NOT is a second testbench GRAPH: the
// harness comes from the same `RfShotTxTB` cut at a different DUT class, so nothing about the
// stimulus, the converter or the sinks differs between the two builds.
//
// WHAT IT WRITES, AND WHY THE BUNDLE NAMES MOVE.
//
// `vectors/resp` and `vectors/rf_out` belong to the default build's run.  This one writes
// `_abs`-suffixed bundles instead, so the two captures can be read back side by side -- which is
// what the negative control needs: the same two assertions, on the same scenario, passing here and
// failing there.
//
// WHAT THIS RUN PROVES that the default build's cannot:
//
//   * THE ADDRESS IS THE PHASE.  Every played word comes out of the address its own absolute word
//     index names, modulo the buffer.  Checked in Python off `vectors/rf_out_abs`.
//   * A PLAYOUT STARTS ON A BOUNDARY.  The load lands mid-pass and the start is deferred to the next
//     `rd == 0`, so the first sample on the wire is the first sample of the waveform AND sits at an
//     absolute index congruent to zero.
//   * THE DEFERRAL COSTS EXACTLY WHAT IT SHOULD.  DAC_UNDERRUN and DAC_BLOCKS_ZERO_FILLED are
//     unchanged: a longer wait is longer FILLER, which is a value the design produces, never a stall.
//
// Regenerate the harness, not this file.
#include <cstdio>

#include "rf_shot_tx_abs_tb_harness.h"

int main() {
    rf_shot_tx_abs_tb::Harness h("rf_shot_tx_abs_counters.wdb");
    // Reassigned BEFORE run(), because the models load and dump in pre_sim / post_sim.
    h.s_in.in_bundle = "vectors/cmd";
    h.resp_out.out_bundle = "vectors/resp_abs";
    h.xsi_tb_dac_if_rx.out_bundle = "vectors/rf_out_abs";
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
