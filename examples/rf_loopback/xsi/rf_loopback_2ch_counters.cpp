// rf_loopback_2ch_counters.cpp -- HAND-WRITTEN.  Runs the generated TWO-CHANNEL RF loopback harness
// and prints the loss counters as KEY=VALUE lines.
//
// The one-channel twin of this file is rf_loopback_counters.cpp, and the difference between them is
// the whole of what Stage A added: there are still exactly TWO converter models here, not four.  One
// `RfdcAdcMaster` spans both `s_in_*` ports plus the RF edge, because the edge carries every
// channel in one block and n_ch models could not each own it; the model takes an `AxisPortList` and
// the harness hands it `{ports::s_in_0, ports::s_in_1}`.
//
// So the counters below come in two flavours, and both are printed:
//
//   * the SUMS (`ADC_DROPPED`, ...) -- "did this converter lose anything", which is the question the
//     one-channel gate asks and which stays comparable across channel counts;
//   * the PER-CHANNEL vectors (`ADC_DROPPED_0`, ...) -- "which port's consumer is the one that
//     cannot keep up", which a sum cannot answer and which is the only way a lane that is quietly
//     starving shows up as itself rather than as half of a larger number.
//
// Regenerate the harness, not this file.
#include <cstdio>

#include "rf_loopback_2ch_tb_harness.h"

int main() {
    rf_loopback_2ch_tb::Harness h("rf_loopback_2ch_counters.wdb");
    // Kept in step with XSI_N_CYCLES in rf_loopback_xsi.py by hand, exactly as the one-channel main
    // is: the number is a testbench bound, not a result, and the result is what the sink collected.
    h.run(16000);

    // The models are named after the FIRST boundary port each spans -- s_in_0 / s_out_0 -- because
    // one object covers the whole group.  That is not cosmetic: two objects here would mean the
    // group did not resolve, and the RF edge would have two owners.
    std::printf("ADC_N_CH=%llu\n", (unsigned long long)h.s_in_0.n_ch());
    std::printf("DAC_N_CH=%llu\n", (unsigned long long)h.s_out_0.n_ch());

    // The ADC presents a beat every cycle its rate is due, whatever TREADY says, and DISCARDS what
    // the fabric will not take -- a real converter cannot stall.
    std::printf("ADC_WORDS_SENT=%llu\n", (unsigned long long)h.s_in_0.words_sent);
    std::printf("ADC_DROPPED=%llu\n",    (unsigned long long)h.s_in_0.dropped);
    for (std::size_t i = 0; i < h.s_in_0.n_ch(); ++i) {
        std::printf("ADC_WORDS_SENT_%d=%llu\n", (int)i,
                    (unsigned long long)h.s_in_0.words_sent_ch[i]);
        std::printf("ADC_DROPPED_%d=%llu\n", (int)i,
                    (unsigned long long)h.s_in_0.dropped_ch[i]);
    }

    // The DAC is always ready; a cycle where a beat was DUE and none came is an underflow, and there
    // is no protocol signal for it.
    std::printf("DAC_WORDS_RECV=%llu\n", (unsigned long long)h.s_out_0.words_recv);
    std::printf("DAC_UNDERRUN=%llu\n",   (unsigned long long)h.s_out_0.underrun);
    for (std::size_t i = 0; i < h.s_out_0.n_ch(); ++i) {
        std::printf("DAC_WORDS_RECV_%d=%llu\n", (int)i,
                    (unsigned long long)h.s_out_0.words_recv_ch[i]);
        std::printf("DAC_UNDERRUN_%d=%llu\n", (int)i,
                    (unsigned long long)h.s_out_0.underrun_ch[i]);
    }
    std::printf("DAC_BLOCKS_OUT=%llu\n", (unsigned long long)h.s_out_0.blocks_out);
    // Blocks the DAC's GRID emitted with nothing to play.  A block is all-or-nothing across the
    // channels: the rows of one block are the same instant on n_ch converters of one tile, so a
    // block assembled from a full row and a short one would claim samples never played together.
    std::printf("DAC_BLOCKS_ZERO_FILLED=%llu\n", (unsigned long long)h.s_out_0.blocks_zero_filled);
    std::printf("DAC_LAST_ZERO_FILL_IDX=%llu\n", (unsigned long long)h.s_out_0.last_zero_fill_idx);

    // The two behavioral edges -- ONE per direction, carrying every channel.
    std::printf("ADC_CHAN_TRANSFERRED=%ld\n", h.xsi_tb_2ch_adc_if.transferred);
    std::printf("ADC_CHAN_DROPPED=%ld\n",     h.xsi_tb_2ch_adc_if.dropped);
    std::printf("DAC_CHAN_TRANSFERRED=%ld\n", h.xsi_tb_2ch_dac_if.transferred);
    std::printf("DAC_CHAN_DROPPED=%ld\n",     h.xsi_tb_2ch_dac_if.dropped);
    std::printf("SRC_BLOCKS_OUT=%llu\n", (unsigned long long)h.xsi_tb_2ch_adc_if_tx.blocks_out);
    std::printf("SINK_BLOCKS_IN=%llu\n", (unsigned long long)h.xsi_tb_2ch_dac_if_rx.blocks_in);
    // Time to last completion -- a RESULT, distinct from the loop bound above it.
    std::printf("SINK_LAST_BLOCK_CYCLE=%llu\n",
                (unsigned long long)h.xsi_tb_2ch_dac_if_rx.last_block_cycle);

    h.close();
    return 0;
}
