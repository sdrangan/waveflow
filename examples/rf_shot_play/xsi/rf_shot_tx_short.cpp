// rf_shot_tx_short.cpp -- HAND-WRITTEN.  The SAME design, the SAME harness, a different scenario:
// a transfer that ends before the shot is full.
//
// WHY A SECOND MAIN AND NOT A SECOND TESTBENCH.
//
// The graph is identical -- same DUT, same converter, same driver and sinks -- and only the input
// bundle differs.  A second testbench *graph* would be a second model of one design, which is the
// trap this arc has paid for more than once; a second main reassigns three bundle names on the
// generated harness and changes nothing else.  Everything the harness knows still comes from the
// component graph.
//
// WHY IT CANNOT BE ANOTHER FRAME IN THE FIRST SCENARIO.
//
// Once a shot is accepted the buffer is BUSY until its play-set finishes, and a file-driven driver
// pushes every frame back to back -- so at most one load per stream can succeed and every later one
// is SHOT_BUSY.  That is not a limitation of the testbench: it is what SHOT_BUSY *is*.  A host that
// wanted two loads would read its verdicts and wait, which a vector file cannot do.  So the short
// load has to be the FIRST frame of a stream, which means a stream of its own.
//
// WHAT IT PROVES.  `TLAST` before the shot is full is the failure the response exists for: a DMA
// reports success either way, so from the host side a half-loaded buffer is indistinguishable from a
// full one.  SHOT_SHORT says which and `nsamp_loaded` says how much -- and nothing plays, which is
// what DAC_BLOCKS_ZERO_FILLED below is asserted on.  Without the TLAST pin this run would not fail,
// it would HANG, waiting for words that are never coming.
//
// Regenerate the harness, not this file.
#include <cstdio>

#include "rf_shot_tx_tb_harness.h"

int main() {
    rf_shot_tx_tb::Harness h("rf_shot_tx_short.wdb");
    // Reassigned BEFORE run(), because the models load and dump in pre_sim / post_sim -- the same
    // lifecycle phases the generated harness sets these in, so this is a different value for the
    // same knob rather than a different mechanism.
    h.s_in.in_bundle = "vectors/cmd_short";
    h.resp_out.out_bundle = "vectors/resp_short";
    h.xsi_tb_dac_if_rx.out_bundle = "vectors/rf_out_short";
    h.run(900);

    std::printf("DAC_WORDS_RECV=%llu\n", (unsigned long long)h.samp_out.words_recv);
    std::printf("DAC_UNDERRUN=%llu\n",   (unsigned long long)h.samp_out.underrun);
    std::printf("DAC_BLOCKS_OUT=%llu\n", (unsigned long long)h.samp_out.blocks_out);
    std::printf("DAC_BLOCKS_ZERO_FILLED=%llu\n",
                (unsigned long long)h.samp_out.blocks_zero_filled);
    std::printf("CMD_SENT=%d\n",  h.s_in.sent());
    std::printf("CMD_TOTAL=%d\n", h.s_in.total());
    std::printf("RESP_WORDS=%zu\n", h.resp_out.count());
    std::printf("RESP_LAST_CYCLE=%ld\n", h.resp_out.cycle_of_word(h.resp_out.count()));
    std::printf("RF_BLOCKS_IN=%llu\n", (unsigned long long)h.xsi_tb_dac_if_rx.blocks_in);

    h.close();
    return 0;
}
