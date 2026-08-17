// rf_blk_delay_counters.cpp -- HAND-WRITTEN.  Runs the generated pattern-B harness and prints the
// model counters as KEY=VALUE lines.
//
// Why this exists beside the generated main.  The generated main runs and dumps, and correctness is
// then checked in Python from the dumped bundles -- a component graph knows which models drive which
// ports and cannot know what you meant to check.  But a pattern-B loop's most important numbers are
// not in any bundle, and there are FOUR of them because a loop can lose samples in four places:
//
//   ADC_DROPPED    words the converter offered that the fabric would not take.  The RX gate: an ADC
//                  cannot be back-pressured, so what the ingress does not accept is GONE.  This is
//                  the number pattern A could not get to zero without a hand-written word-granular
//                  body, and the number RfSampBufRx exists to make structurally zero.
//   DAC_UNDERRUN   sample periods the DAC came due and the fabric had nothing for it.  The TX gate,
//                  and the mirror: a DAC plays whatever is in its FIFO when the period arrives,
//                  including the last word again.
//   *_RESP_WORDS   responses from each buffer -- so "nothing came out" can be told apart from
//                  "nothing was ever commanded", which is the first question when a run is silent.
//
// Neither loss has a protocol event, which is exactly why both are read off the converter models
// rather than from the wire.  Between them sits BlkDelay, which has no counter at all and needs
// none: it can only block, and blocking is not a loss.
//
// Regenerate the harness, not this file.
#include <cstdio>

#include "rf_blk_delay_tb_harness.h"

int main() {
    rf_blk_delay_tb::Harness h("rf_blk_delay_counters.wdb");
    h.run(60000);

    // -- the ADC edge: what the design failed to accept ------------------------------------------
    std::printf("ADC_WORDS_SENT=%llu\n", (unsigned long long)h.s_in.words_sent);
    std::printf("ADC_DROPPED=%llu\n",    (unsigned long long)h.s_in.dropped);
    std::printf("ADC_LAST_DROP_CYCLE=%llu\n", (unsigned long long)h.s_in.last_drop_cycle);

    // -- the DAC edge: what the design failed to supply -------------------------------------------
    std::printf("DAC_WORDS_RECV=%llu\n", (unsigned long long)h.s_out.words_recv);
    std::printf("DAC_UNDERRUN=%llu\n",   (unsigned long long)h.s_out.underrun);
    std::printf("DAC_LAST_UNDERRUN_CYCLE=%llu\n",
                (unsigned long long)h.s_out.last_underrun_cycle);
    std::printf("DAC_BLOCKS_OUT=%llu\n", (unsigned long long)h.s_out.blocks_out);
    std::printf("DAC_BLOCKS_ZERO_FILLED=%llu\n",
                (unsigned long long)h.s_out.blocks_zero_filled);

    // -- the two response streams: one per command, from each buffer ------------------------------
    std::printf("RX_RESP_WORDS=%zu\n", h.rx_resp.count());
    std::printf("RX_RESP_LAST_CYCLE=%ld\n", h.rx_resp.cycle_of_word(h.rx_resp.count()));
    std::printf("TX_RESP_WORDS=%zu\n", h.tx_resp.count());
    std::printf("TX_RESP_LAST_CYCLE=%ld\n", h.tx_resp.cycle_of_word(h.tx_resp.count()));

    // -- the RF edge beyond the converter: blocks that reached the environment ---------------------
    std::printf("RF_BLOCKS_IN=%llu\n", (unsigned long long)h.xsi_tb_dac_if_rx.blocks_in);

    h.close();
    return 0;
}
