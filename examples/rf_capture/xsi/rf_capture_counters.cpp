// rf_capture_counters.cpp -- HAND-WRITTEN.  Runs the generated capture-buffer harness and prints
// the model counters as KEY=VALUE lines.
//
// Why this exists beside the generated main.  The generated main runs and dumps, and correctness is
// then checked in Python from the dumped bundles -- a component graph knows which models drive which
// ports and cannot know what you meant to check.  But two of this design's most important numbers
// are not in any bundle:
//
//   ADC_DROPPED   words the converter offered that the fabric would not take.  THE gate for this
//                 design: the capture buffer exists so that condition 3 of the fidelity contract
//                 (the DUT never stalls its input) holds structurally, and a nonzero count here
//                 means the ingress stalled and the whole point was lost.
//   CMD_SENT      commands the driver actually delivered -- so "no output" can be told apart from
//                 "no input", which is the first question when a run captures nothing.
//
// Regenerate the harness, not this file.
#include <cstdio>

#include "rf_samp_buf_rx_tb_harness.h"

int main() {
    rf_samp_buf_rx_tb::Harness h("rf_capture_counters.wdb");
    h.run(40000);

    // The ADC presents a beat every sample period whatever TREADY says, and DISCARDS what the fabric
    // will not take.  `dropped` is therefore a statement about the DESIGN, not about this model.
    std::printf("ADC_WORDS_SENT=%llu\n", (unsigned long long)h.s_in.words_sent);
    std::printf("ADC_DROPPED=%llu\n",    (unsigned long long)h.s_in.dropped);
    std::printf("ADC_LAST_DROP_CYCLE=%llu\n", (unsigned long long)h.s_in.last_drop_cycle);
    // The command driver and the two capture sinks.
    std::printf("CMD_SENT=%d\n",   h.s_cmd.sent());
    std::printf("CMD_TOTAL=%d\n",  h.s_cmd.total());
    std::printf("OUT_WORDS=%zu\n",  h.s_out.count());
    std::printf("RESP_WORDS=%zu\n", h.s_resp.count());
    // Time to last completion -- a RESULT, distinct from the loop bound above it.
    std::printf("OUT_LAST_CYCLE=%ld\n", h.s_out.cycle_of_word(h.s_out.count()));
    // The RF edge: blocks the source handed the converter, and any it refused.
    std::printf("RF_CHAN_TRANSFERRED=%ld\n", h.xsi_tb_adc_if.transferred);
    std::printf("RF_CHAN_DROPPED=%ld\n",     h.xsi_tb_adc_if.dropped);

    h.close();
    return 0;
}
