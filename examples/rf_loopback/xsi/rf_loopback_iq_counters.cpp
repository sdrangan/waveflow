// rf_loopback_iq_counters.cpp -- HAND-WRITTEN.  Runs the generated INTERLEAVED-I/Q loopback harness
// and prints the loss counters as KEY=VALUE lines.
//
// The real twin of this file is rf_loopback_counters.cpp, and the interesting thing is how little
// differs: the same two converter models, the same two channels, the same counters.  What changed is
// inside a beat -- 2 complex samples in four 16-bit slots instead of 4 real ones -- and the DUT
// between them is `rf_pass_through`, unchanged and not re-synthesized, because a word is a bag of
// bits to the fabric.
//
// So the numbers below are directly comparable to the real run's at the same utilisation, and that
// comparison IS the claim: interleaved I/Q changes what a word holds, not how the converter behaves.
//
// Regenerate the harness, not this file.
#include <cstdio>

#include "rf_loopback_iq_tb_harness.h"

int main() {
    rf_loopback_iq_tb::Harness h("rf_loopback_iq_counters.wdb");
    // Kept in step with XSI_N_CYCLES in rf_loopback_xsi.py by hand, as the other two mains are.
    h.run(16000);

    // The format the models were built with -- printed so the gate can assert that the I/Q rules
    // actually reached the C++ rather than defaulting.  slots_per_word() is 2*samp_per_word here.
    std::printf("FMT_IQ_MODE=%d\n", h.s_in.fmt().iq_mode);
    std::printf("FMT_IQ_ORDER=%d\n", h.s_in.fmt().iq_order);
    std::printf("FMT_SLOTS_PER_WORD=%d\n", h.s_in.fmt().slots_per_word());
    std::printf("FMT_WORD_BITS=%d\n", h.s_in.fmt().word_bits());

    // The ADC presents a beat every cycle its rate is due, whatever TREADY says, and DISCARDS what
    // the fabric will not take -- a real converter cannot stall.
    std::printf("ADC_WORDS_SENT=%llu\n", (unsigned long long)h.s_in.words_sent);
    std::printf("ADC_DROPPED=%llu\n",    (unsigned long long)h.s_in.dropped);
    // The DAC is always ready on its grid; a word due with an empty FIFO is an underflow.
    std::printf("DAC_WORDS_RECV=%llu\n", (unsigned long long)h.s_out.words_recv);
    std::printf("DAC_UNDERRUN=%llu\n",   (unsigned long long)h.s_out.underrun);
    std::printf("DAC_BLOCKS_OUT=%llu\n", (unsigned long long)h.s_out.blocks_out);
    std::printf("DAC_BLOCKS_ZERO_FILLED=%llu\n", (unsigned long long)h.s_out.blocks_zero_filled);
    std::printf("DAC_LAST_ZERO_FILL_IDX=%llu\n", (unsigned long long)h.s_out.last_zero_fill_idx);
    // The two behavioral edges.
    std::printf("ADC_CHAN_TRANSFERRED=%ld\n", h.xsi_tb_iq_adc_if.transferred);
    std::printf("ADC_CHAN_DROPPED=%ld\n",     h.xsi_tb_iq_adc_if.dropped);
    std::printf("DAC_CHAN_TRANSFERRED=%ld\n", h.xsi_tb_iq_dac_if.transferred);
    std::printf("DAC_CHAN_DROPPED=%ld\n",     h.xsi_tb_iq_dac_if.dropped);
    std::printf("SRC_BLOCKS_OUT=%llu\n", (unsigned long long)h.xsi_tb_iq_adc_if_tx.blocks_out);
    std::printf("SINK_BLOCKS_IN=%llu\n", (unsigned long long)h.xsi_tb_iq_dac_if_rx.blocks_in);
    std::printf("SINK_LAST_BLOCK_CYCLE=%llu\n",
                (unsigned long long)h.xsi_tb_iq_dac_if_rx.last_block_cycle);

    h.close();
    return 0;
}
