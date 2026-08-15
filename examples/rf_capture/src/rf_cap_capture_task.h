#ifndef WAVEFLOW_RF_CAP_CAPTURE_TASK_H
#define WAVEFLOW_RF_CAP_CAPTURE_TASK_H
// rf_cap_capture_task.h — the RX capture buffer's CAPTURE side: one RxCmd in, the named window of
// samples out, one RxResp per command.  A single firing per command; the hls::task runtime re-fires.
//
// THIS TASK MAY BLOCK, and that is not a concession — it is the design.  The ingress next door may
// never stall (a converter cannot be back-pressured); this one has nothing upstream of it that loses
// data when it waits, so it is free to wait per sample.  That freedom is what makes all four command
// cases fall out of one loop instead of four code paths.
//
// FOUR CASES, ONE LOOP
//
//   in the buffer   wr-N <= start,  start+nsamp <= wr    served straight out of the buffer
//   in the future   start >= wr                          waits, then serves
//   straddling      start < wr < start+nsamp             pre-trigger from the buffer, then streams
//   too old         start < wr - N                       REFUSED, counted, never a silent read
//
// The straddling case is the one a trigger actually wants — "give me 100 samples around the event" —
// and it needs no code of its own: the loop walks indices and blocks per sample, so the early part
// comes out of the buffer and the late part waits for the converter.
//
// THE HORIZON IS CHECKED PER SAMPLE, NOT PER COMMAND
//
// A long capture with a back-pressured output can start legal and go stale mid-stream: valid when
// you asked, overwritten by the time you read it.  So both bounds are inside the loop.
//
// STALENESS IS SAFE IN ONE DIRECTION AND UNSAFE IN THE OTHER
//
// `last_wr` is a LOWER bound on the true write pointer: the progress channel drops updates rather
// than stalling the converter, so this task's view only ever lags.  That cuts both ways:
//
//   * the "has it been written yet?" test (idx < last_wr) is made HARDER to pass by a stale value,
//     so staleness can only make this task wait longer.  SAFE.
//   * the "has it been overwritten?" test (idx >= last_wr - N) is made EASIER to pass, so a sample
//     that the ingress has already overwritten could slip through.  UNSAFE.
//
// The fix is a MARGIN: the usable horizon is declared as N - MARGIN rather than N, where MARGIN
// bounds how far `last_wr` can lag.  The lag is bounded because this task polls the channel on every
// iteration of the wait loop and once more before every sample it emits, so at most one ingress
// firing's worth of progress can have gone unseen between the poll and its use, plus whatever the
// channel dropped while this task was writing out a sample.  MARGIN is sized by the design (see
// RfSampBufRx.horizon_margin) and is what turns "probably fine" into a stated bound.
//
// The same two inequalities are also what keeps the read address away from the write address: a
// sample is emitted only while `last_wr - N < idx < last_wr <= wr`, so idx and wr are never equal
// modulo N.  bram_t2p.v $errors if that is ever violated, which makes the assertion a live check of
// this logic rather than decoration.
#include "hls_stream.h"
#include <ap_int.h>
#include "rx_cmd.h"
#include "rx_resp.h"

//: Response status codes.  Kept as literals rather than an enum so the generated schema header and
//: this body cannot disagree about the encoding (the schema carries a plain word).
#define RF_CAP_OK       0
#define RF_CAP_TOO_OLD  1

/// @tparam W       sample/word width; also the width of the wrapping sample counter
/// @tparam N       buffer depth in samples (power of two)
/// @tparam MARGIN  samples of horizon given up to bound progress-channel staleness
template <int W, int N, int MARGIN>
static void rf_cap_capture_task(ap_uint<W> buf_r[N], hls::stream<ap_uint<W> >& wr_in,
                                hls::stream<ap_uint<W> >& s_cmd,
                                hls::stream<ap_uint<W> >& s_out,
                                hls::stream<ap_uint<W> >& s_resp) {
    // Survives across commands: what this task last heard about the ingress's position.  Starting at
    // 0 is the honest initial state — "nothing is known to have been written yet".
    static ap_uint<W> last_wr = 0;

    RxCmd c;
    c.read_stream<W>(s_cmd);             // blocks; the capture side is allowed to

    ap_uint<W> idx = c.start;
    ap_uint<W> sent = 0;
    ap_uint<W> status = RF_CAP_OK;

    for (ap_uint<W> i = 0; i < c.nsamp; i = i + 1) {
        // -- 1. wait until sample `idx` has been written -------------------------------------
        //
        // The circular comparison: `idx - last_wr` interpreted as a SIGNED W-bit difference is the
        // position of idx relative to the write pointer, and it stays correct across the counter's
        // wrap as long as the two are within 2^(W-1) of each other -- which the buffer depth and any
        // sane command guarantee.  A plain `idx < last_wr` would break the first time the counter
        // wrapped, and it would break silently.
        bool written = false;
        while (!written) {
            ap_uint<W> w;
            if (wr_in.read_nb(w)) {
                last_wr = w;
            }
            ap_int<W> ahead = (ap_int<W>)(idx - last_wr);
            written = (ahead < 0);
        }

        // -- 2. horizon: refuse a sample that may already have been overwritten ---------------
        ap_int<W> age = (ap_int<W>)(last_wr - idx);        // >= 1 here, by the loop above
        if (age > (ap_int<W>)(N - MARGIN)) {
            status = RF_CAP_TOO_OLD;
            break;                        // no partial nonsense: stop, report, let the host retry
        }

        s_out.write(buf_r[idx & (N - 1)]);
        sent = sent + 1;
        idx = idx + 1;
    }

    RxResp r;
    r.tid = c.tid;
    r.status = status;
    r.nsent = sent;
    r.write_stream<W>(s_resp);
}

#endif  // WAVEFLOW_RF_CAP_CAPTURE_TASK_H
