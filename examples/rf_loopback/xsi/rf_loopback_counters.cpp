// rf_loopback_counters.cpp -- HAND-WRITTEN.  Runs the generated RF loopback harness and prints the
// loss counters as KEY=VALUE lines.
//
// Why this exists beside the generated main.  The generated main runs and dumps; correctness is then
// checked in Python from the dumped bundles, and that split is deliberate -- a component graph knows
// which models drive which ports, and cannot know what you meant to check.  But the counters are not
// in any bundle: they live on the model objects, and there is no dump format for them.
//
// They are also the most interesting output of this testbench.  pysim accounts loss on the RFSampIF
// **edge**, in whole **blocks**; XSI accounts it on the converter models in **words** and **cycles**,
// plus the channel's own block counters.  Writing both down for one scenario is the input to
// plans/behavioral_edges.md S4 (the cross-backend equivalence harness), which is the next step -- so
// this prints them rather than judging them.
//
// Regenerate the harness, not this file.
#include <cstdio>

#include "rf_loopback_tb_harness.h"

int main() {
    rf_loopback_tb::Harness h("rf_loopback_counters.wdb");
    h.run(6000);

    // The ADC presents a beat every cycle its rate is due, whatever TREADY says, and DISCARDS what
    // the fabric will not take -- a real converter cannot stall.  `dropped` is therefore a statement
    // about the DESIGN's ability to keep up, not about this model.
    std::printf("ADC_WORDS_SENT=%llu\n", (unsigned long long)h.s_in.words_sent);
    std::printf("ADC_DROPPED=%llu\n",    (unsigned long long)h.s_in.dropped);
    // The DAC is always ready; a cycle where a beat was DUE and none came is an underflow, and there
    // is no protocol signal for it.
    std::printf("DAC_WORDS_RECV=%llu\n", (unsigned long long)h.s_out.words_recv);
    std::printf("DAC_UNDERRUN=%llu\n",   (unsigned long long)h.s_out.underrun);
    std::printf("DAC_BLOCKS_OUT=%llu\n", (unsigned long long)h.s_out.blocks_out);
    // Blocks the DAC's GRID emitted with nothing to play -- the direct analogue of pysim's
    // RFSampIF.underrun, and the reason the startup transient is visible in RTL at all.
    std::printf("DAC_BLOCKS_ZERO_FILLED=%llu\n", (unsigned long long)h.s_out.blocks_zero_filled);
    std::printf("DAC_LAST_ZERO_FILL_IDX=%llu\n", (unsigned long long)h.s_out.last_zero_fill_idx);
    // The two behavioral edges.  `starved` counts polls that found the queue empty, so on a channel
    // read every cycle it is a poll count rather than a loss -- the number that matters here is
    // `dropped`, which is loss.
    std::printf("ADC_CHAN_TRANSFERRED=%ld\n", h.xsi_tb_adc_if.transferred);
    std::printf("ADC_CHAN_DROPPED=%ld\n",     h.xsi_tb_adc_if.dropped);
    std::printf("DAC_CHAN_TRANSFERRED=%ld\n", h.xsi_tb_dac_if.transferred);
    std::printf("DAC_CHAN_DROPPED=%ld\n",     h.xsi_tb_dac_if.dropped);
    std::printf("SRC_BLOCKS_OUT=%llu\n", (unsigned long long)h.xsi_tb_adc_if_tx.blocks_out);
    std::printf("SINK_BLOCKS_IN=%llu\n", (unsigned long long)h.xsi_tb_dac_if_rx.blocks_in);
    // Time to last completion -- a RESULT, distinct from the loop bound above it.
    std::printf("SINK_LAST_BLOCK_CYCLE=%llu\n",
                (unsigned long long)h.xsi_tb_dac_if_rx.last_block_cycle);

    h.close();
    return 0;
}
