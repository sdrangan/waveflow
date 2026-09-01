#ifndef WAVEFLOW_SHOT_LOOP_LOAD_TASK_H
#define WAVEFLOW_SHOT_LOOP_LOAD_TASK_H
// shot_loop_load_task.h — read a frame, TAKE THE REGION, write it, give it back, answer.
//
// The infinite-play transmitter's command layer (plans/t2p_lock_chan.md S1).  Its Python twin is
// ShotLoopLoad.run_iter, which is the pysim golden and NOT the source of this file.
//
// THE DIFFERENCE FROM shot_tx_load_task.h IS ONE VERDICT AND ONE CHANNEL SET.
//
// That body forwards the payload to a separate buffer task and refuses a load arriving mid-play with
// SHOT_BUSY, because the memory is under a live reader and there is no way to say "stop touching
// it".  This body IS the buffer task: it holds a lock, so it can say exactly that -- and SHOT_BUSY
// therefore has no way to be produced here.  Under infinite play a design that answered BUSY would
// refuse every load forever, which is the capability this whole module exists to add.
//
// SHOT_LOAD IS REFUSED, NOT REINTERPRETED.
//
// A SHOT_LOAD asks for `nrepeat` plays and then quiet, which this design cannot provide.  Answering
// it as though it had been a SHOT_LOOP would be a command answered as something other than what it
// asked for -- and it would be invisible, because the samples would look perfect.  So the opcode is
// checked first and a wrong one is refused like any other malformed command.
//
// THE LOCK IS TAKEN BEFORE THE PAYLOAD IS READ, AND THAT COSTS SOMETHING REAL.
//
// It has to be: `buf[lo + i] = s_in.read()` is one pipeline at II=1 -- a word arrives and a word is
// stored in the same cycle -- and there is no way to have that without owning the region first.  The
// price is that a stalled host holds the region, so the player is in filler for as long as the
// transfer takes rather than for as long as the WRITE takes.  That is correct for this protocol (the
// requester's obligation is to release, not to release promptly) and it is the one thing a user
// should think about that the interface cannot decide for them.
//
// It is also a KNOWN TWIN DIVERGENCE in the other direction: the pysim twin dequeues the whole frame
// before it acquires, because a pysim slave takes a whole burst per `get` and cannot do otherwise.
// So pysim's handover gap is the memory write and the RTL's is the whole transfer.  The two are
// measured separately and neither number is inherited from the other.
//
// A REFUSED HEADER STILL DRAINS ITS PAYLOAD.
//
// The law shot_tx_load_task.h and rf_tx_loader_task.h both state.  Words left on the wire become the
// NEXT header, and every command after that is garbage for reasons that look nothing like the cause.
//
// THE RESET TRAP, AND WHY THIS BODY IS ON THE SAFE SIDE OF IT.
//
// reference-hls-task-reset-trap: an hls::task that WRITES before it READS advances during reset.
// This body opens with a BLOCKING read of the header, so at reset its input is empty and it stalls.
// It also carries no `static` at all -- a transaction lives entirely inside one firing.  The PLAYER
// next door is on the other side of that line and says so.
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "mem_lock.h"
#include "rf_shot_tx_hdr.h"
#include "rf_shot_tx_resp.h"

//: ShotTxResp.status.  Literals rather than an enum, so the generated schema header and this body
//: cannot disagree about the encoding -- rf_tx_loader_task.h's convention.  Guarded because
//: shot_tx_load_task.h defines the same codes and a build ships both files into one directory.
#ifndef SHOT_LOADED
#define SHOT_LOADED    0
#define SHOT_SHORT     1
#define SHOT_WRONG_LEN 2
#define SHOT_BUSY      3
#define SHOT_ZERO_LEN  4
#endif

//: ShotTxHdr.opcode.  SHOT_OP_LOOP is the infinite-play flag and the only one this body accepts.
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
///               host speaks in.  Nothing here reads a word arithmetically.
/// @tparam BASE  first element of the region.  NON-ZERO IS THE INTERESTING CASE: `base + offset` is
///               the shape of the byte-versus-word bug bram_toy stayed green through.
template <int W, int D, int NW, int SPW, int BASE>
static void shot_loop_load_task(hls::stream<streamutils::axi4s_word<W> >& s_in,
                                ap_uint<W> buf[D],
                                memlock::chan& cmd_out,
                                memlock::chan& resp_in,
                                hls::stream<streamutils::axi4s_word<W> >& resp_out) {
    ShotTxHdr h;
    streamutils::tlast_status tl;
    h.read_axi4_stream<W>(s_in, tl);      // BLOCKING, and FIRST -- see the reset note above

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

    ap_uint<16> status = SHOT_LOADED;
    bool accept = false;
    if (h.opcode != SHOT_OP_LOOP) {
        status = SHOT_WRONG_LEN;            // a finite play is not what this design does
    } else if (h.nsamp == 0) {
        status = SHOT_ZERO_LEN;             // nothing to complete on, so it could never resolve
    } else if (h.nsamp != (ap_uint<16>)(NW * SPW)) {
        status = SHOT_WRONG_LEN;            // refused, never truncated
    } else {
        accept = true;
    }

    bool ended = (tl == streamutils::tlast_status::tlast_at_end);
    int took = 0;

    if (accept) {
        memlock::mem_lock_request(cmd_out, LOCK_ACQUIRE,
                                  (ap_uint<28>)BASE, (ap_uint<28>)(BASE + NW));
        ap_uint<28> lo = 0, hi = 0;
        // BLOCKING, and bounded by the player's check_period -- that bound is the whole reason the
        // player declares one.  Without it this read is indistinguishable from a deadlock.
        ap_uint<8> granted = memlock::mem_lock_await(resp_in, lo, hi);
        if (granted != LOCK_GRANTED) {
            // Unreachable while BASE and NW are what the Python checked at construction.  A region
            // the design declared and the memory refuses is a WIRING fault, not a host's mistake --
            // there is nothing useful to tell the host, so it is reported as a malformed command and
            // the payload is drained below rather than left to become the next header.
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
                // Past the frame's end this is the pad -- and it CLOBBERS, because with one region
                // there is nowhere else to put an arriving shot.  SHOT_SHORT is the whole warning.
                buf[lo + i] = x;
            }
            // A BARRIER, not a hint: the player may resume the instant it sees this, so nothing
            // below may touch the region.
            memlock::mem_lock_request(cmd_out, LOCK_RELEASE, lo, hi);
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

    if (accept && took < NW) {
        status = SHOT_SHORT;                // THE verdict this response exists for
    }
    r.status = status;
    // What actually LANDED, not what was asked for -- the number a DMA cannot produce.
    r.nsamp_loaded = accept ? (ap_uint<16>)(took * SPW) : (ap_uint<16>)0;
    r.write_axi4_stream<W>(resp_out);
}

#endif  // WAVEFLOW_SHOT_LOOP_LOAD_TASK_H
