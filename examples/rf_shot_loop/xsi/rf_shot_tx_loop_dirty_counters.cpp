// rf_shot_tx_loop_dirty_counters.cpp -- HAND-WRITTEN.  THE POSITIVE CONTROL'S main.
//
// Identical to rf_shot_tx_loop_counters.cpp except for the harness it includes -- and that one
// difference is the whole reason this file exists.  A harness hardcodes DESIGN_DLL, the path of the
// elaborated snapshot it loads, so a control driven by the SHIPPED design's harness would run the
// shipped design, report the shipped design's numbers, and find no hazard.  That is the single most
// convincing way for a read-during-write gate to lie: everything is green and nothing was measured.
//
// Nothing here is asserted against.  What this run is FOR is the waveform: the same VCD scan, on the
// same manifest shape, must find the collision that shot_loop_play_dirty_task.h causes on purpose --
// and until it does, "no hazards" on the shipped design is not evidence of anything.
//
// Regenerate the harness, not this file.
#include <cstdio>

#include "rf_shot_tx_loop_dirty_tb_harness.h"

int main() {
    rf_shot_tx_loop_dirty_tb::Harness h("rf_shot_tx_loop_dirty_counters.wdb");
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
