// rf_shot_rx_abs_counters.cpp -- HAND-WRITTEN.  The SAME capture scenario, on the SAME design, built
// with `absolute_index = 1` (plans/rf_shot_absolute.md S2).
//
// WHY A SECOND SNAPSHOT AND NOT A SECOND MAIN.
//
// `absolute_index` is a template argument, so the two settings are two pieces of RTL rather than one
// with a mode register in it -- and a gate that only ever elaborated one of them would be asserting
// the mode's behaviour against a simulator.  So this main drives `rf_shot_rx_abs_top`, its own xelab
// snapshot, from its own generated harness.  What it is NOT is a second testbench GRAPH: the harness
// comes from the same `RfShotRxTB` cut at a different DUT class, so nothing about the converter, the
// ramp or the sink differs between the two builds.
//
// WHAT IT WRITES, AND WHY THE BUNDLE NAME MOVES.
//
// `vectors/win` belongs to the default build's run.  This one writes `vectors/win_abs`, so the two
// captures sit beside each other and a reader can put the two window sequences next to one another.
//
// WHAT THIS RUN CAN PROVE, AND WHAT IT CANNOT.
//
// It CAN prove that the mode synthesizes, keeps II=1, keeps the converter fed, and satisfies the
// absolute claim on the wire -- and, the part only an RTL run can say, that TWO REGIONS STILL KEEP
// THE WRITER AND THE READER APART.  That property (plans/t2p_lock_chan.md S2: 140 cycles of both
// ports live, 0 of them in the same region) is what makes the region enforced at RTL by construction
// rather than by an assertion nobody can hear, and absolute indexing moves WHEN a region is claimed
// -- so it is re-measured here rather than inherited.
//
// It CANNOT prove what a DROP does to an address, because the only thing that loses samples on RX is
// a reader that dawdles, and `stall_blocks` is a pysim modelling field that reaches no template
// argument -- a reader that dawdles is not a thing the RTL can be asked to do.  That gate lives
// where the knob does, in tests/hw/test_rf_shot_rx.py, exactly as this arc recorded when S2 of
// plans/t2p_lock_chan.md declined to ship a dirty RTL build for the same reason.
//
// Regenerate the harness, not this file.
#include <cstdio>

#include "rf_shot_rx_abs_tb_harness.h"

int main() {
    rf_shot_rx_abs_tb::Harness h("rf_shot_rx_abs_counters.wdb");
    // Reassigned BEFORE run(), because the models load and dump in pre_sim / post_sim -- the same
    // lifecycle phases the generated harness sets these in.  Only the OUTPUT moves: both builds are
    // driven by the same `vectors/rf_in` the harness already names, which is what makes the two
    // captures comparable at all.
    h.w_out.out_bundle = "vectors/win_abs";
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
