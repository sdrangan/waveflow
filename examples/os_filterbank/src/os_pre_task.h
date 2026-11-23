#ifndef EXAMPLES_OS_FILTERBANK_PRE_TASK_H
#define EXAMPLES_OS_FILTERBANK_PRE_TASK_H
// os_pre_task.h -- the stage in front of the shared FFT (HAND-WRITTEN), the HLS twin of
// examples/os_filterbank/os_filterbank.py::OsPre.  Per frame from the arbiter:
//
//   1. the arbiter's index (s_sel, one word before each frame): input 0 is the feedback, so this
//      frame is a product going back through the FFT -- the inverse;
//   2. the frame's Tag, forwarded to post on the tag link beside the FFT (the FFT frames by count
//      and carries nothing through);
//   3. the direction to the FFT's s_dir, then the NW sample words to its s_in -- raw words, already
//      the FFT's input RadixWord.
//
// WHY THIS SHAPE (plans/ssr_fft_gap_throughput.md, fix 1).  SsrFft takes frames L/R cycles apart only
// back to back: after ANY idle cycle between two frames it takes the next a whole frame-time later
// (docs/guide/dsp/ssr_fft/timing.md).  So this body must put a waiting frame's first word on m_x the
// cycle after the previous frame's last word.  Two things stood in the way, and the shape removes both:
//
//   * A FIRING BOUNDARY.  One frame per firing cost the RTL 6 cycles from a tag to the frame's first
//     word into the FFT, plus the loop's exit and the re-entry.  Here the body is one `while (1)`
//     pipelined at II=1 that never exits -- the shape the FFT's own tasks and rf_samp_buf_player_task
//     use (no boundary: measured in plans/witness/task_loop/).  Every iteration tests empty() before
//     it reads, so a pipeline with nothing to read keeps draining (ssr_fft/synthesis.md, "a blocking
//     read at a while (1) head strands the end of a burst"); the writes block, which is back-pressure.
//
//   * THE TAG'S CYCLE.  The tag travels on s_frm in front of the words, so a frame is NW + 1 reads and
//     at most NW words leave in NW + 1 cycles: one cycle without a word is unavoidable.  Where it falls
//     decides what it costs.  Between frames it costs the FFT a frame-time.  Inside a frame it costs
//     one cycle -- at some positions only: a hole moves through each commutator by (R-1)*D words, and
//     one that reaches the frame-sized group of tp{S-2}, cm0 or rc at its boundary costs a frame again
//     (just before the last word, for one).  Between words 0 and 1 it costs one cycle at L = 64, 256
//     and 1024 (a cycle model of the chain from cycle_ref.CommutatorTask; the RTL is the check).  So:
//     every word but the frame's first leaves one cycle after it is read (a one-word skid, `held`);
//     the first bypasses it; the cycle that reads the next frame's tag writes the previous frame's
//     last word.  The hole falls between words 0 and 1.
//
// The decision (both heads waiting) and its copies are one iteration; the direction is computed there
// and written on the next, with the first word (loop_optimization.md: do not decide, compute and commit
// in one iteration).  A frame not waiting when the last word is due does not hold it: it leaves alone
// on that cycle, so an isolated frame is never stranded.  Nothing is written that was not read.
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "os_filterbank_config.h"
#include "tag.h"

namespace os_pre_body {

// One iteration of the loop: at most one word read and one written on each stream.  Its own function so
// a logic check can run it a cycle at a time (tests/examples/test_os_filterbank.py, under g++).
template <int NW, typename TF, typename TS, typename TT>
static inline void step(hls::stream<TF>& s_frm, hls::stream<TS>& s_sel,
                        hls::stream<ap_uint<OS_W_LOOP> >& m_x, hls::stream<ap_uint<8> >& m_dir,
                        hls::stream<TT>& m_tag) {
#pragma HLS INLINE
    const int W = OS_W_LOOP;
    static_assert(Tag::nwords<OS_W_LOOP>() == 1, "os_pre: the tag is one word of the loop's width");
    static bool in_frame = false;               // the frame's words are being read (else: a head next)
    static ap_uint<8> w = 0;                     // the next word's index in the frame
    static bool have = false;                    // `held` is a word owed to m_x
    static bool inv = false;                     // this frame's direction, from its index
    static ap_uint<OS_W_LOOP> held = 0;          // the skid: the last word read, not yet written
#pragma HLS reset variable=in_frame
#pragma HLS reset variable=w
#pragma HLS reset variable=have

    bool put = false;                            // a word to m_x this iteration, and which
    ap_uint<OS_W_LOOP> out = held;
    if (!in_frame) {
        if (have) {                              // the previous frame's last word: leaves now, either way
            put = true;
            have = false;
        }
        if (!s_frm.empty() && !s_sel.empty()) {  // the next frame's head is here: take it beside that word
            bool last;
            const ap_uint<OS_W_LOOP> sel = streamutils::read_boundary_word<TS, W>(s_sel, last);
            Tag t;
            streamutils::read_msg<OS_W_LOOP>(t, s_frm);
            streamutils::write_msg<OS_W_LOOP>(t, m_tag);
            inv = (sel == 0);
            in_frame = true;
            w = 0;
        }
    } else if (!s_frm.empty()) {
        bool last;
        const ap_uint<OS_W_LOOP> x = streamutils::read_boundary_word<TF, W>(s_frm, last);
        if (w == 0) {                            // the first word: straight through, with the direction
            m_dir.write(inv ? ap_uint<8>(1) : ap_uint<8>(0));
            out = x;
            put = true;
        } else {                                 // the rest: a cycle behind (word 1's cycle is the hole)
            put = have;
            held = x;
            have = true;
        }
        if (w == NW - 1) {
            in_frame = false;
            w = 0;
        } else {
            w = w + 1;
        }
    }
    if (put) m_x.write(out);
}

}  // namespace os_pre_body

template <int NW, typename TF, typename TS, typename TT>
static void os_pre_task(hls::stream<TF>& s_frm, hls::stream<TS>& s_sel,
                        hls::stream<ap_uint<OS_W_LOOP> >& m_x, hls::stream<ap_uint<8> >& m_dir,
                        hls::stream<TT>& m_tag) {
    // One word a cycle, forever: no firing boundary (see the header).  Vitis warns that the loop is
    // infinite ([XFORM 203-561]); that is the shape.
    while (1) {
#pragma HLS PIPELINE II=1
        os_pre_body::step<NW>(s_frm, s_sel, m_x, m_dir, m_tag);
    }
}

#endif  // EXAMPLES_OS_FILTERBANK_PRE_TASK_H
