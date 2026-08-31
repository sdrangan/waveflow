#ifndef WAVEFLOW_SHOT_TX_LOAD_TASK_H
#define WAVEFLOW_SHOT_TX_LOAD_TASK_H
// shot_tx_load_task.h — read a header, decide, forward the payload, answer.  ONE response per header.
//
// The shot transmitter's command layer (plans/rf_shot_buf.md, Stage B).  Its Python twin is
// ShotTxLoad.run_iter, which is the pysim golden and NOT the source of this file.
//
// THE TLAST PIN IS THE MECHANISM, NOT A CONVENIENCE.
//
// `s_in` is a framed_word stream, so the kernel really has an `s_in_TLAST` pin (see
// waveflow.hw.interface.FramedStreamIFSlave -- every other free-running top in this repo has a plain
// ap_uint boundary stream and therefore no such pin).  It is here because a payload word and a
// header word are the same W bits: without a frame boundary there is no in-band way to say "that was
// the end", so a host that sends fewer words than its header declared would simply stall the
// buffer's counted loop.  A hang is indistinguishable from a deadlock, and it is invisible from the
// host side because the DMA reports success either way.  With the pin, a short transfer is a VERDICT
// ON THE DATA PATH -- which is the whole reason § "The response is not optional" exists.
//
// FOUR REFUSALS, FOUR DIFFERENT REPAIRS, AND MALFORMED IS TESTED BEFORE TRANSIENT.
//
// SHOT_ZERO_LEN and SHOT_WRONG_LEN are faults in the COMMAND: decidable from the header alone and
// answered before a single payload word is taken.  SHOT_BUSY is a fault in the TIMING -- a load
// arriving while a shot is playing -- and it is refused rather than queued, because accepting it
// would overwrite the memory under the reader, which is exactly the overlap ShotPhase and
// bram_t2p.v's $error exist to make unreachable.  The order is rf_tx_loader_task.h's, for its
// reason: a command that is wrong AND badly timed should be told the thing it can fix.  Only
// SHOT_SHORT cannot be reached from the header -- it is what the STREAM turned out to be.
//
// A REFUSED HEADER STILL DRAINS ITS PAYLOAD.
//
// The same law rf_tx_loader_task.h states.  Words left on the wire become the NEXT header, and every
// command after that is garbage for reasons that look nothing like the cause.  So the loop below
// runs whatever the verdict; only the pay_out write is conditional.
//
// AN ACCEPTED-BUT-SHORT SHOT IS PADDED, AND THEN NOT PLAYED.
//
// rf_shot_buf_load_task.h's inner loop is COUNTED (NW words, no early exit), which is exactly why it
// reaches II=1 where the streaming buffer cannot, and it is RTL-gated as it stands.  So a short
// frame is completed with zeros: the buffer fills, emits its one token, and the design stays live.
// The zeros never reach the converter because a short shot is handed a repeat count of ZERO -- the
// token still has to be consumed (nothing else will take it), but half a waveform must not play.
// That is why the verdict and the repeat count travel together.
//
// THE RESET TRAP, AND WHY THIS BODY IS ON THE SAFE SIDE OF IT.
//
// `busy` is a static, and a static's `= 0` is a simulation initial value in the RTL, not a reset
// (reference-hls-task-reset-trap).  What makes a task actually advance during reset is BEGINNING
// WITH A WRITE: rf_tx_loader_task.h opens with a non-blocking harvest and a non-blocking poll, so
// nothing stalls it.  This body opens with a BLOCKING read of the header, so at reset its input is
// empty, it stalls, and `busy` cannot move.  The pragma is here anyway, and the generated tcl
// carries `config_rtl -reset state` -- which is what actually closed it under Vitis 2025.1 when the
// pragma alone did not (measured in rf_repeat_play).
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "rf_shot_tx_hdr.h"
#include "rf_shot_tx_resp.h"

//: ShotTxResp.status.  Literals rather than an enum, so the generated schema header and this body
//: cannot disagree about the encoding -- rf_tx_loader_task.h's convention, for its reason.  Each code
//: is a distinct REPAIR; collapsing any two would report one fault as another.
#define SHOT_LOADED    0
#define SHOT_SHORT     1
#define SHOT_WRONG_LEN 2
#define SHOT_BUSY      3
#define SHOT_ZERO_LEN  4

//: ShotTxHdr.opcode.
#define SHOT_OP_LOAD 0
#define SHOT_OP_END  1

/// @tparam W    word width in bits -- the host port, the memory and the converter, one number.
/// @tparam NW   words in one shot.  BUILD-TIME STRUCTURE and the single source for the length: the
///              header's `nsamp` is what the HOST believes, and catching the two disagreeing is what
///              SHOT_WRONG_LEN is.
/// @tparam SPW  samples one word carries -- what turns a word count into the `nsamp` a host speaks
///              in.  Only used to translate; nothing here reads a word arithmetically.
template <int W, int NW, int SPW>
static void shot_tx_load_task(hls::stream<streamutils::framed_word<W> >& s_in,
                              hls::stream<ap_uint<W> >& done_in,
                              hls::stream<ap_uint<W> >& pay_out,
                              hls::stream<ap_uint<W> >& rep_out,
                              hls::stream<streamutils::framed_word<W> >& resp_out) {
    // Shots accepted and not yet finished playing.  ONE BIT is enough: a second load is refused
    // while it is set, so at most one `done` can ever be outstanding and there is nothing to count.
    static ap_uint<1> busy = 0;
#pragma HLS reset variable=busy

    ShotTxHdr h;
    streamutils::tlast_status tl;
    h.read_framed_stream<W>(s_in, tl);      // BLOCKING, and FIRST -- see the reset note above

    // Harvested AFTER the header has arrived, so `busy` is as fresh as it can be when it is read.
    // NON-BLOCKING: a play still running is the ANSWER (SHOT_BUSY), not a reason to wait.
    ap_uint<W> tok;
    if (done_in.read_nb(tok)) {
        busy = 0;
    }

    ShotTxResp r;
    r.tid = h.tid;

    if (h.opcode == SHOT_OP_END) {
        // A FENCE, not a halt.  An hls::task has no loop to break -- the runtime re-fires this body
        // forever and an ap_ctrl_none design has no `return` to reach -- so what END is worth is what
        // its RESPONSE proves: headers are answered strictly in order, so this one says everything
        // ahead of it has been processed.  A testbench that ended by timing out instead could not
        // tell a finished run from a deadlocked one.  The frame is the header, so there is nothing
        // to drain.
        r.status = SHOT_LOADED;
        r.nsamp_loaded = 0;
        r.write_framed_stream<W>(resp_out);
        return;
    }

    ap_uint<16> status = SHOT_LOADED;
    bool accept = false;
    if (h.nsamp == 0) {
        status = SHOT_ZERO_LEN;             // nothing to complete on, so it could never resolve
    } else if (h.nsamp != (ap_uint<16>)(NW * SPW)) {
        status = SHOT_WRONG_LEN;            // refused, never truncated: a shorter waveform is a
                                            // different signal, not a smaller version of this one
    } else if (busy) {
        status = SHOT_BUSY;                 // transient, and the only one a retry repairs
    } else {
        accept = true;
    }

    // ONE COUNTED PASS DOES ALL THREE JOBS: forward, drain, pad.
    //
    // Counted (`i < NW`) with PIPELINE II=1, so there is no data-dependent TRIP COUNT for Vitis to
    // refuse to flatten.  `ended` is a data-dependent CONDITION inside the body, which is a
    // different thing and was measured at II=1 (plans/witness/task_loop).  A header beat that
    // already carried TLAST is an empty frame -- `tlast_at_end` on a one-word header -- so the loop
    // reads nothing and pads everything.
    bool ended = (tl == streamutils::tlast_status::tlast_at_end);
    int took = 0;
    for (int i = 0; i < NW; i++) {
#pragma HLS PIPELINE II=1
        ap_uint<W> x = 0;
        if (!ended) {
            streamutils::framed_word<W> fw = s_in.read();
            x = fw.data;
            took = i + 1;
            if (fw.last) ended = true;
        }
        if (accept) {
            pay_out.write(x);               // past the frame's end this is the pad
        }
    }

    // A frame LONGER than the shot.  Unbounded and therefore unpipelined -- deliberately: it runs
    // only on a malformed frame, it is outside every pipelined region, and the alternative (leaving
    // the residue) is the desynchronisation the drain exists to prevent, arrived at from the other
    // side.
    while (!ended) {
        streamutils::framed_word<W> fw = s_in.read();
        if (fw.last) ended = true;
    }

    if (accept && took < NW) {
        status = SHOT_SHORT;                // THE verdict this response exists for
    }
    if (accept) {
        // Zero repeats for a shot that is not playable: the buffer's token still has to be consumed.
        rep_out.write((status == SHOT_LOADED) ? (ap_uint<W>)h.nrepeat : (ap_uint<W>)0);
        busy = 1;
    }

    r.status = status;
    // What actually LANDED, not what was asked for -- the number a DMA cannot produce.
    r.nsamp_loaded = accept ? (ap_uint<16>)(took * SPW) : (ap_uint<16>)0;
    r.write_framed_stream<W>(resp_out);
}

#endif  // WAVEFLOW_SHOT_TX_LOAD_TASK_H
