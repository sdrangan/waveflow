#ifndef WAVEFLOW_PINGPONG_CAPTURE_TASK_H
#define WAVEFLOW_PINGPONG_CAPTURE_TASK_H
// pingpong_capture_task.h — take a block every firing; put it in a free region, or DROP it and say so.
//
// The continuous-capture receiver's owner (plans/t2p_lock_chan.md S2).  Its Python twin is
// PingPongCapture.run_iter, which is the pysim golden and NOT the source of this file.
//
// THE OWNER HERE READS BEFORE IT WRITES, AND THAT IS THE OPPOSITE OF TX.
//
// reference-hls-task-reset-trap: an hls::task that WRITES before it READS advances during reset.
// shot_tx_player_task.h is on the wrong side of that line and cannot help it -- a TX owner produces
// samples nobody asked for, which is what "the side that cannot stop" means there.  An RX owner
// CONSUMES them, so its first act is a blocking stream read and it stalls at reset like any
// requester.  The statics still carry `#pragma HLS reset` because they are state and a reset should
// clear them; what they do not need is the solution-level `config_rtl -reset state` that TX needs.
//
// THE INPUT IS CONSUMED WHATEVER THE ANSWER IS.
//
// `samp_in.read()` is unconditional inside the loop and the WRITE is what the guard covers.  A body
// that read only when it had room would be back-pressuring an ADC -- which is not a thing that can
// happen -- and it would make `dropped` read ZERO for a design that was quietly losing everything
// one stage upstream instead.
//
// NO LOCAL COPY OF THE BLOCK, AND THAT IS THE MEASUREMENT.
//
// The obvious body reads BW words into a local array, decides, then stores them.  That is a whole
// second buffer for no reason: the destination is known BEFORE the block arrives, because it depends
// only on statics the previous firing left behind.  So the store is one pipeline at II=1 -- a word
// arrives and a word is stored in the same cycle -- which is what the pysim twin says by writing
// with the anchor its read returned.
//
// `wp` IS EITHER A FILL POINTER OR A TIMESTAMP, AND `ABS` IS WHICH (plans/rf_shot_absolute.md S2).
//
// At ABS=0 -- the default, and every earlier version's behaviour -- `wp` is FILL-DRIVEN: it does not
// advance on a drop, so an address says HOW FAR INTO THIS CAPTURE and a lost block shifts everything
// after it.  At ABS=1 it advances and wraps on every firing, dropped blocks included, so it says HOW
// MANY WORDS SINCE RESET, modulo D -- and A DROP LEAVES A HOLE RATHER THAN A SHIFT: the blocks
// either side of it are still at the addresses their own indices name.
//
// The mirror of shot_tx_player_task.h's `rd`, and the same split: the ADVANCE leaves the guard, the
// ANNOUNCEMENT does not.  A region nothing was written into is not a window, and handing one out
// would publish the previous pass's samples under this pass's header.
//
// AT ABS=1 THE REGION SEARCH COLLAPSES TO ONE QUESTION, ASKED ONCE PER REGION.
//
// Which region a block belongs to is `wp / RW`, a function of the block's own index rather than of
// reader timing.  So the only thing left to decide is whether that region is FREE, and that is asked
// at the region's first block and held for the whole region -- which is what keeps an announced
// window from ever being partially written.  It also bounds the loss differently: a region that is
// busy at its boundary is skipped ENTIRELY, where the default mode retries every block.  That is the
// cost, and it is the RX mirror of the deferral TX pays.
//
// A REGION IS FREE WHEN IT IS RELEASED, NOT WHEN IT IS TAKEN.
//
// `full_r[i]` says region i holds samples nobody has read; `held_r[i]` says the reader has it right
// now.  Only a RELEASE clears either.  If taking a region freed it, this task could refill the half
// a reader is still draining, which is the collision arrived at from the bookkeeping rather than
// from the lock.
//
// AND THE TASK TRACKS `held_r` ITSELF, WHICH pysim DOES NOT HAVE TO.
//
// The pysim twin asks the lock endpoint (`may_touch`), because there the endpoint is also the GUARD
// that raises on a violation.  There is no guard at RTL -- plans/t2p_lock_chan.md says so under
// "a grant is not a fence at RTL" -- so this body keeps the same fact in its own register.  The two
// are twins in behaviour and not in mechanism, which is stated here rather than left to be noticed.
#include "hls_stream.h"
#include <ap_int.h>
#include "mem_lock.h"
#include "capture_window_hdr.h"

//: CaptureWindowHdr.status.  Literals rather than an enum, so the generated schema header and this
//: body cannot disagree about the encoding -- the convention every task body in this arc follows.
#ifndef CAP_OK
#define CAP_OK   0
#define CAP_LOST 1
#endif

//: Width of the cumulative drop counter, matching CaptureWindowHdr.n_dropped.  The counter is kept
//: at the FIELD's width so the wrap happens in the same place in both backends: a wider register
//: would publish a truncated value while believing an exact one.
#ifndef CAP_DROP_W
#define CAP_DROP_W 28
#endif

/// @tparam W   word width in bits -- the converter's, the memory's, one number.
/// @tparam D   memory depth in elements.  The `mode=bram` array's size, which is what makes the
///             pragma take effect (an unsized pointer degrades to an ap_vld scalar port).
/// @tparam NR  regions the memory is split into.  Two at S2; three would need an allocator.
/// @tparam BW  words per block: the pipelined loop's trip count AND the poll period, because they
///             are one boundary.  Must divide D/NR, so a block never straddles a region.
/// @tparam ABS THE INDEX IS A TIMESTAMP (plans/rf_shot_absolute.md S2).  0 is every earlier
///             version's behaviour: `wp` is fill-driven and a drop shifts every address after it.
///             1 makes `wp` advance UNCONDITIONALLY and wrap at D, so it is this task's own word
///             count since reset and a drop leaves a HOLE at the addresses the lost blocks named.
///
///             ONE BODY, NOT TWO.  It is a template argument and the branches are `if (ABS)`, so
///             Vitis folds the constant and each build synthesizes exactly the design it asked for.
///             Two copied bodies would be two designs that drift.
///
///             D/NR must be a power of two when this is 1: the region is `wp / RW` and the boundary
///             test is `wp % RW`, which are a shift and a mask at a power of two and a DIVIDER on
///             the datapath's critical path otherwise.  PingPongCapture refuses the geometry.
template <int W, int D, int NR, int BW, int ABS>
static void pingpong_capture_task(hls::stream<ap_uint<W> >& samp_in,
                                  ap_uint<W> buf[D],
                                  memlock::chan& cmd_in,
                                  memlock::chan& resp_out,
                                  hls::stream<ap_uint<W> >& rdy_out) {
    //: The region being filled, and how far into it.
    static ap_uint<8> cur = 0;
#pragma HLS reset variable=cur
    static ap_uint<32> wp = 0;
#pragma HLS reset variable=wp
    //: Per region: holds samples nobody has read / the reader has it right now.  See the header note
    //: -- only a RELEASE clears either.
    static ap_uint<1> full_r[NR] = {};
#pragma HLS reset variable=full_r
    static ap_uint<1> held_r[NR] = {};
#pragma HLS reset variable=held_r
    //: Words lost since reset, and the value as it stood at the last announcement.  The difference
    //: is what turns a cumulative count into the per-window verdict.
    static ap_uint<CAP_DROP_W> dropped = 0;
#pragma HLS reset variable=dropped
    static ap_uint<CAP_DROP_W> announced = 0;
#pragma HLS reset variable=announced
    //: ABS only: the region `cur` names was CLAIMED at its boundary.  Asked once per region and held
    //: for all BW-word blocks of it, so a region is filled entirely or skipped entirely.
    static ap_uint<1> claimed = 0;
#pragma HLS reset variable=claimed

    const int RW = D / NR;                       // elements in one region

    // WHERE THIS BLOCK GOES, DECIDED BEFORE IT ARRIVES.  It depends only on what the previous firing
    // left behind, which is what lets the store below be one pipeline with no local copy.
    bool have;
    if (ABS) {
        // THE REGION IS A FUNCTION OF THE INDEX, AND THE ONLY QUESTION LEFT IS WHETHER IT IS FREE.
        // `wp` advances unconditionally at the bottom and wraps at D, so it is this task's word
        // count since reset modulo the memory; `wp / RW` is therefore the region absolute block k
        // belongs to, and `wp % RW == 0` is that region's FIRST block -- an exact test, hit exactly
        // once per region, because BW divides RW.
        if (wp % RW == 0) {
            cur = (ap_uint<8>)(wp / RW);
            claimed = (!full_r[cur] && !held_r[cur]) ? (ap_uint<1>)1 : (ap_uint<1>)0;
        }
        have = (claimed != 0);
    } else {
        have = (!full_r[cur]) && (!held_r[cur]) && (wp + BW <= (ap_uint<32>)((cur + 1) * RW));
        if (!have) {
            int nxt = -1;
            // DOWNWARD, so the LAST assignment wins and `nxt` ends up the LOWEST free region.  The
            // pysim twin scans upward and takes the first; an upward loop here would take the last
            // and the two backends would ping-pong in opposite orders on a three-region design.  It
            // costs nothing to agree and it is invisible at NR=2, which is why it is written down.
        find_region:
            for (int i = NR - 1; i >= 0; i--) {
#pragma HLS UNROLL
                if (!full_r[i] && !held_r[i]) nxt = i;
            }
            if (nxt >= 0) {
                cur = (ap_uint<8>)nxt;
                wp = (ap_uint<32>)(nxt * RW);
                have = true;
            }
        }
    }

    ap_uint<32> dst = wp;
    // LABELLED, and that is not decoration: Vitis names an unlabelled loop VITIS_LOOP_<line>_1 and
    // nests that name into its children, so a comment edit above renames the synthesized module --
    // and a gate looking the II up by name then MISSES and skips, which reads as a pass.
store_block:
    for (int i = 0; i < BW; i++) {
#pragma HLS PIPELINE II=1
        ap_uint<W> x = samp_in.read();           // UNCONDITIONAL -- see the header note
        if (have) {
            buf[dst + i] = x;
        }
    }

    if (!have) {
        // NOWHERE TO PUT IT.  The block is gone, for the one reason RX has and TX does not: the
        // reader is holding, or has not drained, the region this task needs.
        dropped += BW;
    }

    // THE ADVANCE, AND THE SPLIT.  Under ABS the pointer moves on EVERY firing, dropped blocks
    // included -- that is what makes it a timestamp and a drop a hole.  What must NOT come out of
    // the guard with it is the ANNOUNCEMENT, which stays on `have`.
    if (ABS || have) {
        wp += BW;
        if (wp >= (ap_uint<32>)((cur + 1) * RW) && have) {
            // The region is complete.  Announce it, and decide the verdict HERE, where the answer is
            // known: anything lost since the last announcement fell immediately before this window.
            full_r[cur] = 1;
            CaptureWindowHdr h;
            h.status = (dropped != announced) ? (ap_uint<8>)CAP_LOST : (ap_uint<8>)CAP_OK;
            // STILL AN ADDRESS, still CAP_DROP_W bits wide.  The lock speaks in addresses and the
            // reader turns this straight into an acquire; ABS changes which address a block gets,
            // never what the field means.
            h.base_addr = (ap_uint<CAP_DROP_W>)(cur * RW);
            h.n_dropped = dropped;
            announced = dropped;
            // A BLOCKING write, and it cannot block: at most NR regions can be full at once, so at
            // most NR announcements can be outstanding, and the channel is that deep.  A task that
            // stalled here would be back-pressuring an ADC.
            h.write_stream<W>(rdy_out);
        }
        if (ABS && wp >= (ap_uint<32>)D) {
            // THE WRAP, and it is the whole of this design's absolute addressing.  D is a power of
            // two, so at RTL it is a mask.
            wp = 0;
        }
    }

    // EXACTLY ONE POLL, outside everything above -- that is what `check_period` means, and it is
    // what keeps the datapath's II untouched.
    MemLockCmd c;
    if (memlock::mem_lock_poll(cmd_in, c)) {
        int idx = (int)(c.start_addr) / RW;
        if (idx >= 0 && idx < NR) {
            if (c.opcode == LOCK_ACQUIRE) {
                held_r[idx] = 1;                 // STOP TOUCHING IT ...
                memlock::mem_lock_grant<D>(resp_out, c.start_addr, c.end_addr);   // ... THEN grant
            } else {
                held_r[idx] = 0;
                full_r[idx] = 0;                 // the RELEASE is what makes a region reusable
            }
        }
    }
}

#endif  // WAVEFLOW_PINGPONG_CAPTURE_TASK_H
