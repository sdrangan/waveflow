// rf_pingpong_rx_counters.cpp -- HAND-WRITTEN.  Runs the generated harness and prints the model
// counters as KEY=VALUE lines.
//
// Why this exists beside the generated main.  The generated main runs and dumps, and correctness is
// then checked in Python from the dumped bundles -- a component graph knows which models drive which
// ports and cannot know what you meant to check.  But this design's most important numbers are in no
// bundle:
//
//   ADC_DROPPED   words the CONVERTER could not hand over because the fabric would not take them.
//                 This is the OTHER loss, and it is not the one the design counts: an ADC that could
//                 not push a word into the kernel lost it before the capture ever saw it.  It must
//                 be ZERO -- the whole premise of a capture front end is that it absorbs the
//                 converter's rate -- and a run where it is not is measuring the wrong failure.
//   ADC_WORDS     words the converter did hand over.  The denominator for everything else.
//   WIN_WORDS / WIN_LAST_CYCLE  the windows the host got, and when the last one landed.  A RESULT,
//                 distinct from the run's loop bound, and the number the gate records.
//
// The design's OWN drop count is not here, and deliberately: it rides on every window's header, so
// the gate reads it off the captured bundle where a host would.  A counter printed here would be a
// second place for it to live.
//
// Regenerate the harness, not this file.
#include <cstdio>

#include "rf_pingpong_rx_tb_harness.h"

int main() {
    rf_pingpong_rx_tb::Harness h("rf_pingpong_rx_counters.wdb");
    h.run(2800);

    // The converter side: what it presented, and what the fabric would not take.
    std::printf("ADC_WORDS=%llu\n",   (unsigned long long)h.samp_in.words_sent);
    std::printf("ADC_DROPPED=%llu\n", (unsigned long long)h.samp_in.dropped);
    std::printf("ADC_BLOCKS_IN=%llu\n", (unsigned long long)h.xsi_tb_adc_if_tx.blocks_out);

    // The host side: the windows, and when the last word of the last one arrived.
    std::printf("WIN_WORDS=%zu\n", h.w_out.count());
    std::printf("WIN_LAST_CYCLE=%ld\n", h.w_out.cycle_of_word(h.w_out.count()));

    h.close();
    return 0;
}
