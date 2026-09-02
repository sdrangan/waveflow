#ifndef WAVEFLOW_SHOT_TX_LOADER_TASK_H
#define WAVEFLOW_SHOT_TX_LOADER_TASK_H
// shot_tx_loader_task.h — read a frame, decide, take the region, write it, hand it back, answer.
//
// The shot transmitter's command layer (plans/rf_shot_unify.md).  Its Python twin is
// ShotTxLoader.run_iter, which is the pysim golden and NOT the source of this file.
//
// IT IS shot_loop_load_task.h PLUS THE TWO THINGS THE FINITE PATH NEEDS.
//
// Those two are `busy` and the play command's opcode, and between them they are the whole merge:
//
//   busy       a FINITE shot is in flight.  Set after a SHOT_LOAD is accepted, cleared by the `done`
//              token the player sends when the play-set ends.  While it is set EVERY load is refused
//              -- a SHOT_LOOP too, because the objection is not to what the arriving shot is, it is
//              that truncating the running one would be invisible.  A preempted three-pass shot
//              produces two perfectly good passes and every counter downstream still adds up.
//   pc.opcode  forwarded to the player so it knows WHO IS WAITING.  A SHOT_LOAD owes a `done`; a
//              SHOT_LOOP must not send one, because a spurious token would clear a `busy` that a
//              LATER finite shot set and the next load would preempt it.
//
// AN INFINITE SHOT IS PREEMPTED, AND THAT IS THE OTHER HALF.
//
// `busy` is set only by SHOT_LOAD, so a load of either kind arriving while a LOOP plays is accepted
// and takes the lock.  A design that set it for both would answer SHOT_BUSY forever after the first
// loop -- which is the defect the infinite-play predecessor was written to avoid.
//
// A SHORT SHOT IS LOADED AND THEN NOT PLAYED, ON EITHER PATH.
//
// `pc.nrepeat` is zero unless the verdict is SHOT_LOADED.  RfShotTx achieves this with a repeat count
// of zero and the infinite-play predecessor could not -- it played the padded result, because it
// had no way to go quiet.
// This design has one, so the stricter rule wins.  A short FINITE shot still sets `busy` and is still
// owed a `done`: the player sends it immediately on seeing the release.
//
// THE PLAY COMMAND GOES OUT BEFORE THE RELEASE.
//
// The player reads it on the release branch, so ordering the two writes this way makes that read a
// bounded wait rather than a guess.  Even a scheduler that reordered them would be correct, only
// slower by a beat -- the player blocks for it either way.
//
// THE RESET TRAP, AND WHY THIS BODY IS ON THE SAFE SIDE OF IT.
//
// reference-hls-task-reset-trap: an hls::task that WRITES before it READS advances during reset.
// This body opens with a BLOCKING read of the header, so at reset its input is empty and it stalls.
// `busy` carries #pragma HLS reset anyway, because it is state a reset should clear.
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "mem_lock.h"
#include "rf_shot_tx_hdr.h"
#include "rf_shot_tx_resp.h"
#include "shot_play_cmd.h"

//: ShotTxResp.status.  Literals rather than an enum, so the generated schema header and this body
//: cannot disagree about the encoding -- the convention every task body in this family follows.
//: Guarded because a build may ship the predecessors' bodies into the same directory.
#ifndef SHOT_LOADED
#define SHOT_LOADED    0
#define SHOT_SHORT     1
#define SHOT_WRONG_LEN 2
#define SHOT_BUSY      3
#define SHOT_ZERO_LEN  4
#endif

//: ShotTxHdr.opcode.
#ifndef SHOT_OP_LOAD
#define SHOT_OP_LOAD 0
#define SHOT_OP_END  1
#endif
#ifndef SHOT_OP_LOOP
#define SHOT_OP_LOOP 2
#endif

/// @tparam W     word width in bits -- the host port's and the memory's.
/// @tparam D     memory depth in elements.  The `mode=bram` array's size, which is what makes the
///               pragma take effect (an unsized pointer degrades to an ap_vld scalar port).
/// @tparam NW    words in one shot.  BUILD-TIME STRUCTURE and the single source for the length: the
///               header's `nsamp` is what the HOST believes, and catching the two disagreeing is
///               what SHOT_WRONG_LEN is.
/// @tparam SPW   samples one word carries -- only used to translate a word count into the `nsamp` a
///               host speaks in.
/// @tparam BASE  first element of the region.  NON-ZERO IS THE INTERESTING CASE: `base + offset` is
///               the shape of the byte-versus-word bug bram_toy stayed green through.
template <int W, int D, int NW, int SPW, int BASE>
static void shot_tx_loader_task(hls::stream<streamutils::axi4s_word<W> >& s_in,
                                hls::stream<ap_uint<W> >& done_in,
                                ap_uint<W> buf[D],
                                memlock::chan& cmd_out,
                                memlock::chan& resp_in,
                                hls::stream<ap_uint<W> >& rep_out,
                                hls::stream<streamutils::axi4s_word<W> >& resp_out) {
    //: A FINITE shot is in flight.  ONE BIT is enough: a second load is refused while it is set, so
    //: at most one `done` can ever be outstanding and there is nothing to count.
    static ap_uint<1> busy = 0;
#pragma HLS reset variable=busy

    ShotTxHdr h;
    streamutils::tlast_status tl;
    h.read_axi4_stream<W>(s_in, tl);      // BLOCKING, and FIRST -- see the reset note above

    // Harvested AFTER the header has arrived, so `busy` is as fresh as it can be when it is read.
    // NON-BLOCKING: a finite shot still running is the ANSWER (SHOT_BUSY), not a reason to wait.
    ap_uint<W> tok;
    if (done_in.read_nb(tok)) {
        busy = 0;
    }

    ShotTxResp r;
    r.tid = h.tid;

    if (h.opcode == SHOT_OP_END) {
        // A FENCE, not a halt.  An hls::task has no loop to break, so what END is worth is what its
        // RESPONSE proves: headers are answered strictly in order, so this one says everything ahead
        // of it has been processed.  The frame is the header, so there is nothing to drain.
        r.status = SHOT_LOADED;
        r.nsamp_loaded = 0;
        r.write_axi4_stream<W>(resp_out);
        return;
    }

    // MALFORMED BEFORE TRANSIENT, which is this repo's order and for its reason: a command that is
    // wrong AND badly timed should be told the thing it can fix.  Retry repairs a BUSY; nothing
    // repairs a length the buffer was not built for.
    ap_uint<16> status = SHOT_LOADED;
    bool accept = false;
    if (h.opcode != SHOT_OP_LOAD && h.opcode != SHOT_OP_LOOP) {
        status = SHOT_WRONG_LEN;            // refused, never reinterpreted
    } else if (h.nsamp == 0) {
        status = SHOT_ZERO_LEN;             // nothing to complete on, so it could never resolve
    } else if (h.nsamp != (ap_uint<16>)(NW * SPW)) {
        status = SHOT_WRONG_LEN;            // refused, never truncated
    } else if (busy) {
        status = SHOT_BUSY;                 // transient, and the only one a retry repairs
    } else {
        accept = true;
    }

    bool ended = (tl == streamutils::tlast_status::tlast_at_end);
    int took = 0;

    if (accept) {
        memlock::mem_lock_request(cmd_out, LOCK_ACQUIRE,
                                  (ap_uint<28>)BASE, (ap_uint<28>)(BASE + NW));
        ap_uint<28> lo = 0, hi = 0;
        // Bounded by the player's check_period -- that bound is the whole reason the player declares
        // one.  Implemented as a read_nb poll loop inside mem_lock_await; a plain blocking read here
        // would be scheduled into the request's state and deadlock (plans/t2p_lock_chan.md S1).
        ap_uint<8> granted = memlock::mem_lock_await(resp_in, lo, hi);
        if (granted != LOCK_GRANTED) {
            // Unreachable while BASE and NW are what the Python checked at construction.  A region
            // the design declared and the memory refuses is a WIRING fault, not a host's mistake --
            // so it is reported as malformed and the payload is drained below rather than left to
            // become the next header.
            accept = false;
            status = SHOT_WRONG_LEN;
        } else {
            // ONE COUNTED PASS DOES ALL THREE JOBS: store, drain, pad.  Counted (`i < NW`) with
            // PIPELINE II=1, so there is no data-dependent TRIP COUNT for Vitis to refuse to
            // flatten; `ended` is a data-dependent CONDITION inside the body, which is a different
            // thing and was measured at II=1.
            //
            // LABELLED, and that is not decoration: Vitis names an unlabelled loop
            // VITIS_LOOP_<line>_1 and nests that into its children, so a comment edit above renames
            // the synthesized module -- and a gate looking the II up by name then MISSES and skips,
            // which reads as a pass.
        take_shot:
            for (int i = 0; i < NW; i++) {
#pragma HLS PIPELINE II=1
                ap_uint<W> x = 0;
                if (!ended) {
                    streamutils::axi4s_word<W> fw = s_in.read();
                    x = fw.data;
                    took = i + 1;
                    if (fw.last) ended = true;
                }
                // `lo` is the GRANT's base, not BASE: what the owner yielded is what may be touched.
                // Past the frame's end this is the pad.
                buf[lo + i] = x;
            }
            if (took < NW) {
                status = SHOT_SHORT;        // THE verdict this response exists for
            }
            // WHAT TO PLAY, decided here and sent BEFORE the release.  nrepeat is zero unless the
            // shot is whole, so half a waveform never reaches the converter on either path.
            ShotPlayCmd pc;
            pc.opcode = h.opcode;
            pc.nrepeat = (status == SHOT_LOADED) ? (ap_uint<16>)h.nrepeat : (ap_uint<16>)0;
            pc.write_stream<W>(rep_out);
            // A BARRIER, not a hint: the player may resume the instant it sees this.
            memlock::mem_lock_request(cmd_out, LOCK_RELEASE, lo, hi);
            if (h.opcode == SHOT_OP_LOAD) {
                // Only a FINITE shot makes the design busy, and only a finite shot is owed a `done`.
                busy = 1;
            }
        }
    }

    // A frame LONGER than the shot, or the payload of a refused one.  Unbounded and therefore
    // unpipelined -- deliberately: it runs only on a malformed frame, it is outside every pipelined
    // region, and the alternative (leaving the residue) is the desynchronisation it exists to
    // prevent, arrived at from the other side.
drain_tail:
    while (!ended) {
        streamutils::axi4s_word<W> fw = s_in.read();
        if (fw.last) ended = true;
    }

    r.status = status;
    // What actually LANDED, not what was asked for -- the number a DMA cannot produce.
    r.nsamp_loaded = accept ? (ap_uint<16>)(took * SPW) : (ap_uint<16>)0;
    r.write_axi4_stream<W>(resp_out);
}

#endif  // WAVEFLOW_SHOT_TX_LOADER_TASK_H
