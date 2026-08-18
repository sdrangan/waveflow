#ifndef WAVEFLOW_RF_SAMP_BUF_LOADER_TASK_H
#define WAVEFLOW_RF_SAMP_BUF_LOADER_TASK_H
// rf_samp_buf_loader_task.h — the TX sample buffer's LOADER: one TxCmd in, its payload in-band
// behind it, into the circular buffer.  One firing per command; the hls::task runtime re-fires.
//
// THE MIRROR OF rf_samp_buf_capture_task.h, AND THE ASYMMETRY IS THE DESIGN.
//
// RX exists because an ADC cannot be back-pressured: its ingress may never refuse a write, and it
// fails by OVERRUNNING.  TX has the opposite obligation.  A DAC consumes a word every sample period
// whether or not you have one for it, so the PLAYER's obligation is never to miss a deadline and it
// fails by UNDERRUNNING.  This task is the other half: like the RX capture it **may block**, because
// nothing downstream of it misses a deadline while it waits -- the player keeps playing whatever the
// loader is doing.  Do not copy the never-stall law onto this task; it belongs next door.
//
// IN-BAND PAYLOAD: ONE PORT, COMMAND THEN DATA.
//
// The samples arrive on the SAME stream as the command, immediately behind it -- the mem_copy /
// interleaver framed shape, which is XSI-proven (plans/adc_model.md: "make the in-band variant
// primary").  There is no data_addr and no m_axi here.
//
// THE FRAME MUST BE DRAINED WHATEVER THE VERDICT.  A refused command whose payload is left in the
// stream desynchronises every command after it -- the next read would take a sample for a tid.  So
// the loop below ALWAYS consumes NPAY words and the status only decides whether each one is stored.
// That is why there is no early `break` here, unlike the RX capture, which had no payload to drain.
//
// WHICH DIRECTION STALENESS HURTS, AND WHY THE MARGIN IS ON THE OTHER TEST THAN RX'S.
//
// `last_rd` is a LOWER bound on the player's true position: the progress channel drops rather than
// stalling the player.  So:
//
//   * "is there room -- has the player moved far enough that writing this slot will not clobber a
//     sample it has not played yet?" is made HARDER to pass by a stale value, so staleness only
//     makes this task wait longer.  SAFE.
//   * "has this slot already played?" is made EASIER to pass, so a slot the player has in fact
//     already sent could be written -- data for a moment that has gone.  UNSAFE.
//
// So MARGIN guards the too-late test: a slot is accepted only if it leads the last-known play
// position by at least MARGIN samples.  Note this is the mirror image of the RX capture, where the
// margin guarded the horizon test; the unsafe direction moves with the direction of dataflow.
#include "hls_stream.h"
#include <ap_int.h>
#include "tx_cmd.h"
#include "tx_resp.h"

//: Response status codes.  Kept as literals rather than an enum so the generated schema header and
//: this body cannot disagree about the encoding, and shared with the RX bodies so one table serves
//: both directions.
#define RF_SAMP_BUF_OK          0
#define RF_SAMP_BUF_TOO_OLD     1
#define RF_SAMP_BUF_MISALIGNED  2
#define RF_SAMP_BUF_TOO_LATE    3

/// @tparam W       AXIS word width in bits
/// @tparam SPW     samples per word (power of two)
/// @tparam N       buffer depth in WORDS (power of two); the buffer holds N*SPW samples
/// @tparam MARGIN  SAMPLES of lead a slot must have over the last-known play position
/// @tparam IDX_W   width of the sample-index counter and of every TxCmd/TxResp field
template <int W, int SPW, int N, int MARGIN, int IDX_W>
static void rf_samp_buf_loader_task(ap_uint<W> buf_w[N], hls::stream<ap_uint<W> >& rd_in,
                                    hls::stream<ap_uint<W> >& s_in,
                                    hls::stream<ap_uint<W> >& wr_out,
                                    hls::stream<ap_uint<W> >& s_resp) {
    // Survives across commands: what this task last heard about the player's position.  0 is the
    // honest initial state -- "the player is not known to have played anything yet".
    static ap_uint<IDX_W> last_rd = 0;

    TxCmd c;
    c.read_stream<W>(s_in);              // blocks; the loader side is allowed to

    ap_uint<IDX_W> idx = c.start;
    ap_uint<IDX_W> loaded = 0;
    ap_uint<IDX_W> status = RF_SAMP_BUF_OK;

    // Payload length is a property of the FRAME, so it is rounded UP and drained in full whatever
    // the verdict.  At SPW == 1 this is exactly c.nsamp and the rounding folds away.
    ap_uint<IDX_W> npay = (c.nsamp + SPW - 1) / SPW;

    // Word alignment, decided before anything is stored.  Refused rather than rounded: a rounded
    // window plays samples at the wrong moment, and nothing downstream could detect it.  At SPW == 1
    // both operands are 0 and the whole branch folds away.
    if ((c.start % SPW) != 0 || (c.nsamp % SPW) != 0) {
        status = RF_SAMP_BUF_MISALIGNED;
    }

    // -- 1. WAIT FOR ROOM ONCE, FOR THE WHOLE FRAME, BEFORE the drain loop -------------------
    //
    // This wait used to sit INSIDE the per-word loop, and that nesting is what cost this body its
    // II.  Vitis said so exactly:
    //
    //   [HLS 200-878] Unable to schedule the loop exit test ('icmp_ln99') in the first pipeline
    //                 iteration (II = 1 cycles)
    //   [HLS 200-960] Cannot flatten loop 'VITIS_LOOP_85_1' ... sub loop is do-while
    //   [HLS 200-1470] Target II = NA, Final II = 2, loop 'VITIS_LOOP_99_2'
    //
    // -- i.e. it pipelined the INNER spin at II=2 and could not flatten the payload loop into it.
    // The RX ingress does the same essential work (stream-read -> BRAM-write) and reaches II=1
    // precisely because it has no inner loop.
    //
    // Waiting for the whole frame up front is SAFE RATHER THAN EQUIVALENT, and safe in the
    // conservative direction: it waits for at least as much room as the per-word version ever did,
    // because the last word of the frame needs the most.  A frame is at most a block and the buffer
    // is many blocks, so the wait is the same wait in practice -- what changed is where it sits.
    //
    // The circular comparison: `idx - last_rd` as a SIGNED IDX_W-bit difference is a slot's lead
    // over the play pointer, and it stays correct across the counter's wrap as long as the two are
    // within 2^(IDX_W-1) of each other.  A plain `<` would break the first time the counter wrapped,
    // and it would break silently.
    if (status == RF_SAMP_BUF_OK) {
        bool room = false;
        while (!room) {
            ap_uint<W> r;
            if (rd_in.read_nb(r)) {
                last_rd = (ap_uint<IDX_W>)r;
            }
            // The END of the frame is what has to fit, not its start.
            ap_int<IDX_W> lead_end = (ap_int<IDX_W>)(idx + (ap_uint<IDX_W>)(npay * SPW) - last_rd);
            room = (lead_end < (ap_int<IDX_W>)(N * SPW));
        }
    }

    for (ap_uint<IDX_W> i = 0; i < npay; i = i + 1) {
#pragma HLS PIPELINE II=1
        // ALWAYS consume, so the frame stays aligned even for a refused command.
        ap_uint<W> x = s_in.read();
        if (status != RF_SAMP_BUF_OK) {
            continue;                    // refused: consumed and discarded, never silently stored
        }

        // Keep the play position fresh for the too-late test below.  Non-blocking, so no loop.
        ap_uint<W> r2;
        if (rd_in.read_nb(r2)) {
            last_rd = (ap_uint<IDX_W>)r2;
        }

        // -- 2. too late: the slot has already played, or is within MARGIN of having played ----
        ap_int<IDX_W> lead = (ap_int<IDX_W>)(idx - last_rd);
        if (lead < (ap_int<IDX_W>)MARGIN) {
            status = RF_SAMP_BUF_TOO_LATE;
            continue;                    // keep draining; the frame is still owed its words
        }

        buf_w[(idx / SPW) & (N - 1)] = x;   // a BRAM port: no handshake, cannot refuse
        loaded = loaded + SPW;
        idx = idx + SPW;
        // Tell the player how far the buffer is filled.  Non-blocking for the same reason the RX
        // ingress's is: only the newest position means anything, and a blocking write here would
        // make the loader wait on a channel the player drains only once per firing.
        wr_out.write_nb((ap_uint<W>)idx);
    }

    TxResp r;
    r.tid = c.tid;
    r.status = status;
    r.nloaded = loaded;
    r.write_stream<W>(s_resp);
}

#endif  // WAVEFLOW_RF_SAMP_BUF_LOADER_TASK_H
