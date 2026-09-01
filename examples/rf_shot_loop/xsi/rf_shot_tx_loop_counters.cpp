// rf_shot_tx_loop_counters.cpp -- HAND-WRITTEN.  Runs the generated harness and prints the model
// counters as KEY=VALUE lines.
//
// Why this exists beside the generated main.  The generated main runs and dumps, and correctness is
// then checked in Python from the dumped bundles -- a component graph knows which models drive which
// ports and cannot know what you meant to check.  But this design's most important numbers are in no
// bundle:
//
//   DAC_UNDERRUN   sample periods the DAC came due and the fabric had nothing for it.  THE claim of
//                  an infinite-play design is that a HANDOVER does not starve the converter -- it
//                  makes it play silence, briefly.  There is no protocol signal for "you were late",
//                  so this counter is the only evidence either simulator has, and it must be ZERO
//                  after the startup transient: the filler is a VALUE, not a stall.
//   DAC_BLOCKS_ZERO_FILLED  blocks the grid had to fill itself.  Distinct from the design's own
//                  filler, which arrives as real beats and is not a zero-fill at all.  A design
//                  that stalled instead of filling would show up HERE and nowhere else.
//   RESP_LAST_CYCLE  the cycle the last verdict reached its sink.  A RESULT, distinct from the run's
//                  loop bound, and the number the gate records.
//
// Regenerate the harness, not this file.
#include <cstdio>

#include "rf_shot_tx_loop_tb_harness.h"

int main() {
    rf_shot_tx_loop_tb::Harness h("rf_shot_tx_loop_counters.wdb");
    h.run(1400);

    // The DAC plays whatever is in its FIFO when a sample period comes due, INCLUDING nothing.
    // `underrun` and `blocks_zero_filled` are therefore statements about the DESIGN feeding it.
    std::printf("DAC_WORDS_RECV=%llu\n", (unsigned long long)h.samp_out.words_recv);
    std::printf("DAC_UNDERRUN=%llu\n",   (unsigned long long)h.samp_out.underrun);
    std::printf("DAC_LAST_UNDERRUN_CYCLE=%llu\n",
                (unsigned long long)h.samp_out.last_underrun_cycle);
    std::printf("DAC_BLOCKS_OUT=%llu\n", (unsigned long long)h.samp_out.blocks_out);
    std::printf("DAC_BLOCKS_ZERO_FILLED=%llu\n",
                (unsigned long long)h.samp_out.blocks_zero_filled);
    std::printf("DAC_LAST_ZERO_FILL_IDX=%llu\n",
                (unsigned long long)h.samp_out.last_zero_fill_idx);

    // The command driver and the response sink: the in-band frames in, the verdicts out.
    std::printf("CMD_SENT=%d\n",  h.s_in.sent());
    std::printf("CMD_TOTAL=%d\n", h.s_in.total());
    std::printf("RESP_WORDS=%zu\n", h.resp_out.count());
    std::printf("RESP_LAST_CYCLE=%ld\n", h.resp_out.cycle_of_word(h.resp_out.count()));

    // The RF edge beyond the converter: blocks that reached the environment.
    std::printf("RF_BLOCKS_IN=%llu\n", (unsigned long long)h.xsi_tb_dac_if_rx.blocks_in);

    h.close();
    return 0;
}
