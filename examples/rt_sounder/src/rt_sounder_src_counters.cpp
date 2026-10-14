// rt_sounder_src_counters.cpp -- HAND-WRITTEN.  Runs the generated source-mode harness (the street
// canyon's received signal, replayed from RtSignalSource's rendered bundle through rf_pass_through)
// and prints the format and the loss counters as KEY=VALUE lines.
//
// The twin of examples/rf_loopback/xsi/rf_loopback_iq_counters.cpp: the same converter models, the
// same DUT, the same geometry and the same cycle budget, so the counters are directly comparable
// with that gate's recorded expectations.  What differs is only what the source replays.
//
// Authored, so it lives in src/ (plans/source_layout.md): rt_sounder_xsi.generate_tb copies it into
// xsi/, which is generated and gitignored.  Regenerate the harness, not this file.
#include <cstdio>

#include "rt_sounder_src_tb_harness.h"

int main() {
    rt_sounder_src_tb::Harness h("rt_sounder_src_counters.wdb");
    // Kept in step with XSI_N_CYCLES (rf_loopback_xsi.py) by hand, as the loopback's mains are.
    h.run(16000);

    std::printf("FMT_IQ_MODE=%d\n", h.s_in.fmt().iq_mode);
    std::printf("FMT_IQ_ORDER=%d\n", h.s_in.fmt().iq_order);
    std::printf("FMT_SLOTS_PER_WORD=%d\n", h.s_in.fmt().slots_per_word());
    std::printf("FMT_WORD_BITS=%d\n", h.s_in.fmt().word_bits());

    std::printf("ADC_WORDS_SENT=%llu\n", (unsigned long long)h.s_in.words_sent);
    std::printf("ADC_DROPPED=%llu\n",    (unsigned long long)h.s_in.dropped);
    std::printf("DAC_WORDS_RECV=%llu\n", (unsigned long long)h.s_out.words_recv);
    std::printf("DAC_UNDERRUN=%llu\n",   (unsigned long long)h.s_out.underrun);
    std::printf("DAC_BLOCKS_OUT=%llu\n", (unsigned long long)h.s_out.blocks_out);
    std::printf("DAC_BLOCKS_ZERO_FILLED=%llu\n", (unsigned long long)h.s_out.blocks_zero_filled);
    std::printf("DAC_LAST_ZERO_FILL_IDX=%llu\n", (unsigned long long)h.s_out.last_zero_fill_idx);
    std::printf("ADC_CHAN_TRANSFERRED=%ld\n", h.src_tb_adc_if.transferred);
    std::printf("ADC_CHAN_DROPPED=%ld\n",     h.src_tb_adc_if.dropped);
    std::printf("DAC_CHAN_TRANSFERRED=%ld\n", h.src_tb_dac_if.transferred);
    std::printf("DAC_CHAN_DROPPED=%ld\n",     h.src_tb_dac_if.dropped);
    std::printf("SRC_BLOCKS_OUT=%llu\n", (unsigned long long)h.src_tb_adc_if_tx.blocks_out);
    std::printf("SINK_BLOCKS_IN=%llu\n", (unsigned long long)h.src_tb_dac_if_rx.blocks_in);
    std::printf("SINK_LAST_BLOCK_CYCLE=%llu\n",
                (unsigned long long)h.src_tb_dac_if_rx.last_block_cycle);

    h.close();
    return 0;
}
