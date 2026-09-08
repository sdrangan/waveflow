// rf_shot_tx_abs_loop.cpp -- HAND-WRITTEN.  The INFINITE scenario at `absolute_index = 1`, and it
// is here to measure THE COST rather than the feature.
//
// `plans/rf_shot_absolute.md` states the price of deferral exactly: "The cost is latency, bounded by
// one pass."  In this scenario that bound BITES.  Every load in `vectors/cmd_loop` is preempted by
// the next one before its deferred start arrives -- the gaps between accepts are shorter than one
// pass at this geometry -- so under absolute indexing NOTHING is played and the whole run is filler.
//
// That is the design working, and it is worth an RTL gate for two reasons:
//
//   * IT IS THE COST, MEASURED.  A reader of the mode needs a run where the bound is reached, not
//     only runs where it is comfortably met.  The default build plays waveform A, a gap, then B on
//     this identical stream; the difference between the two captures IS the latency this mode buys
//     its phase property with.
//   * IT EXERCISES THE DISARM AT RTL.  Each preemption arrives while a shot is ARMED but not yet
//     playing, so the ACQUIRE has to clear `pending` and not merely `playing`.  A stale arm would
//     start the OLD waveform at the next boundary, out of a memory the new load has already
//     rewritten -- and the capture would then contain samples belonging to neither waveform.  An
//     empty capture is the assertion that it does not.
//
// Every verdict is still answered, and the converter is still never starved: quiet is a VALUE.
//
// Regenerate the harness, not this file.
#include <cstdio>

#include "rf_shot_tx_abs_tb_harness.h"

int main() {
    rf_shot_tx_abs_tb::Harness h("rf_shot_tx_abs_loop.wdb");
    h.s_in.in_bundle = "vectors/cmd_loop";
    h.resp_out.out_bundle = "vectors/resp_loop_abs";
    h.xsi_tb_dac_if_rx.out_bundle = "vectors/rf_out_loop_abs";
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
