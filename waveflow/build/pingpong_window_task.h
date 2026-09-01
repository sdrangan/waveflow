#ifndef WAVEFLOW_PINGPONG_WINDOW_TASK_H
#define WAVEFLOW_PINGPONG_WINDOW_TASK_H
// pingpong_window_task.h — wait to be told a region is ready, take it, drain it, give it back.
//
// The continuous-capture receiver's requester (plans/t2p_lock_chan.md S2).  Its Python twin is
// PingPongWindow.run_iter, which is the pysim golden and NOT the source of this file.
//
// IT BLOCKS ON `rdy` BEFORE IT ASKS FOR ANYTHING, AND THAT ORDERING IS WHY THE CHANNEL EXISTS.
//
// The lock answers "may I touch these addresses".  It has no way to say "there is something there
// worth touching" -- so a reader that alternated blindly would acquire a half the capture had not
// filled and drain zeros, and zeros out of a capture buffer look exactly like a quiet signal.  The
// announcement is the synchronisation the lock deliberately does not provide.
//
// THE HEADER IS FORWARDED VERBATIM, AND THIS TASK IS NOT ITS AUTHOR.
//
// It did not see the drops and has no way to.  Re-deriving the verdict here would make two authors
// of one statement, which is the arrangement in which they disagree.
//
// ONE FRAME: THE HEADER BEAT, THEN THE SAMPLES, TLAST ON THE LAST ONE.
//
// A host reads a window as one DMA transfer, so the statement about the samples has to travel inside
// the same frame.  The pysim twin concatenates the serialized header onto the payload and writes ONE
// burst for exactly this reason: two writes are two bursts and therefore two TLASTs, and the two
// backends would then disagree about the frame BOUNDARY rather than about a value -- the harder kind
// of divergence to see.
//
// THE GRANT WAIT IS A POLL LOOP, NOT A BLOCKING READ.
//
// That is not a style choice; it is mem_lock.h's, and it cost S1 a full RTL debug.  A write followed
// by a blocking read on a DIFFERENT stream has no data dependency, so Vitis schedules both into one
// state -- and a state that stalls on the empty response FIFO performs none of its writes, so the
// request is never sent.  `mem_lock_await` polls with read_nb inside a loop, which is a scheduling
// barrier.  This body just uses it.
//
// AND IT IS ON THE SAFE SIDE OF THE RESET TRAP.
//
// Its first act is a blocking read of the announcement, so at reset its input is empty and it
// stalls.  It also carries no `static` at all: a window lives entirely inside one firing.
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "mem_lock.h"
#include "capture_window_hdr.h"

/// @tparam W   word width in bits.
/// @tparam D   memory depth in elements.
/// @tparam NR  regions the memory is split into.
/// @tparam BW  words per block.  Unused by this body and taken anyway, so the two halves of the pair
///             are instantiated from ONE set of template arguments -- a window task told a different
///             geometry from its capture is a design whose two ends disagree silently.
template <int W, int D, int NR, int BW>
static void pingpong_window_task(hls::stream<ap_uint<W> >& rdy_in,
                                 ap_uint<W> buf[D],
                                 memlock::chan& cmd_out,
                                 memlock::chan& resp_in,
                                 hls::stream<streamutils::axi4s_word<W> >& w_out) {
    const int RW = D / NR;                       // elements in one region, and in one window

    CaptureWindowHdr h;
    h.read_stream<W>(rdy_in);                    // BLOCKING, and FIRST -- see the reset note above

    ap_uint<28> base = h.base_addr;
    memlock::mem_lock_request(cmd_out, LOCK_ACQUIRE, base, (ap_uint<28>)(base + RW));

    ap_uint<28> lo = 0, hi = 0;
    // BLOCKING in effect, and bounded by the capture's check_period -- that bound is the whole reason
    // the capture declares one.  Implemented as a poll loop; see the header note.
    ap_uint<8> st = memlock::mem_lock_await(resp_in, lo, hi);

    if (st == LOCK_GRANTED) {
        // The header beat, NOT last: the frame is the header plus the window.
        h.write_axi4_stream<W>(w_out, false);
        // LABELLED -- see the note in pingpong_capture_task.h.  The II gate looks this module up by
        // that label.
    drain_window:
        for (int i = 0; i < RW; i++) {
#pragma HLS PIPELINE II=1
            // `lo` is the GRANT's base, not `base`: what the owner yielded is what may be touched.
            streamutils::write_axi4_word<W>(w_out, buf[lo + i], i == RW - 1);
        }
        // A BARRIER, not a hint: the capture may refill the region the instant it sees this, so
        // nothing below may touch it.
        memlock::mem_lock_request(cmd_out, LOCK_RELEASE, lo, hi);
    }
}

#endif  // WAVEFLOW_PINGPONG_WINDOW_TASK_H
