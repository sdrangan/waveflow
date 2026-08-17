// rf_samp_buf_tx_counters.cpp -- HAND-WRITTEN.  Runs the generated playout harness and prints the
// model counters as KEY=VALUE lines.
//
// Why this exists beside the generated main.  The generated main runs and dumps, and correctness is
// then checked in Python from the dumped bundles -- a component graph knows which models drive which
// ports and cannot know what you meant to check.  But this design's most important numbers are not
// in any bundle, and for TX they are the mirror of the RX ones:
//
//   DAC_UNDERRUN   sample periods the DAC came due and the fabric had nothing for it.  THE gate for
//                  this design: a playout buffer exists so the player never misses a deadline, and
//                  a nonzero count in steady state means it did.  It is read from a counter on the
//                  converter model because a DAC underrun has no protocol event -- there is no
//                  signal for "you were late", which is exactly why RX counts drops and TX counts
//                  underruns rather than either being observable on the wire.
//   RESP_WORDS     responses the loader emitted -- so "nothing played" can be told apart from
//                  "nothing was ever commanded", which is the first question when a run is silent.
//
// Regenerate the harness, not this file.
#include <cstdio>

#include "rf_samp_buf_tx_tb_harness.h"

int main() {
    rf_samp_buf_tx_tb::Harness h("rf_samp_buf_tx_counters.wdb");
    h.run(40000);

    // The DAC plays whatever is in its FIFO when a sample period comes due, INCLUDING nothing.
    // `underrun` and `blocks_zero_filled` are therefore statements about the DESIGN feeding it.
    std::printf("DAC_WORDS_RECV=%llu\n",  (unsigned long long)h.s_out.words_recv);
    std::printf("DAC_UNDERRUN=%llu\n",    (unsigned long long)h.s_out.underrun);
    std::printf("DAC_LAST_UNDERRUN_CYCLE=%llu\n",
                (unsigned long long)h.s_out.last_underrun_cycle);
    std::printf("DAC_BLOCKS_OUT=%llu\n",  (unsigned long long)h.s_out.blocks_out);
    std::printf("DAC_BLOCKS_ZERO_FILLED=%llu\n",
                (unsigned long long)h.s_out.blocks_zero_filled);

    // The command driver and the response sink: the in-band frame, in and out.
    std::printf("CMD_SENT=%d\n",   h.s_in.sent());
    std::printf("CMD_TOTAL=%d\n",  h.s_in.total());
    std::printf("RESP_WORDS=%zu\n", h.s_resp.count());
    std::printf("RESP_LAST_CYCLE=%ld\n", h.s_resp.cycle_of_word(h.s_resp.count()));

    // The RF edge beyond the converter: blocks that reached the environment.
    std::printf("RF_BLOCKS_IN=%llu\n", (unsigned long long)h.xsi_tb_dac_if_rx.blocks_in);

    h.close();
    return 0;
}
