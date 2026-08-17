#ifndef WAVEFLOW_RF_SAMP_BUF_CAPTURE_TASK_H
#define WAVEFLOW_RF_SAMP_BUF_CAPTURE_TASK_H
// rf_samp_buf_capture_task.h — the RX sample buffer's CAPTURE side: one RxCmd in, the named window of
// samples out, one RxResp per command.  A single firing per command; the hls::task runtime re-fires.
//
// THIS TASK MAY BLOCK, and that is not a concession — it is the design.  The ingress next door may
// never stall (a converter cannot be back-pressured); this one has nothing upstream of it that loses
// data when it waits, so it is free to wait per word.  That freedom is what makes all four command
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
// and it needs no code of its own: the loop walks indices and blocks per word, so the early part
// comes out of the buffer and the late part waits for the converter.
//
// WINDOWS ARE WORD-ALIGNED, AND THE REFUSAL IS EXPLICIT
//
// RxCmd names a window in SAMPLE index, but a word carries SPW samples and this loop emits whole
// words.  A sub-word window would mean unpacking, selecting and re-packing inside a loop that must
// stay cheap; that is real work and it is deliberately not done here.  So `start` and `nsamp` must
// both be multiples of SPW, and a window that is not is refused with RF_SAMP_BUF_MISALIGNED rather
// than being silently rounded — a rounded window is data from the wrong time, which is the one
// failure mode a capture buffer must never have.  At SPW == 1 the test folds away to a constant.
//
// THE HORIZON IS CHECKED PER WORD, NOT PER COMMAND
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
//   * the "has it been overwritten?" test (idx >= last_wr - N*SPW) is made EASIER to pass, so a
//     sample the ingress has already overwritten could slip through.  UNSAFE.
//
// The fix is a MARGIN: the usable horizon is declared as N*SPW - MARGIN samples rather than N*SPW,
// where MARGIN bounds how far `last_wr` can lag.  The lag is bounded because this task polls the
// channel on every iteration of the wait loop and once more before every word it emits, so at most
// one ingress firing's worth of progress can have gone unseen between the poll and its use, plus
// whatever the channel dropped while this task was writing out a word.  MARGIN is sized by the
// design (see RfSampBufCapture.horizon_margin) and turns "probably fine" into a stated bound.
//
// The same two inequalities are also what keeps the read address away from the write address: a word
// is emitted only while `last_wr - N*SPW < idx < last_wr <= wr`, so their addresses are never equal
// modulo N.  bram_t2p.v $errors if that is ever violated, which makes the assertion a live check of
// this logic rather than decoration.
#include "hls_stream.h"
#include <ap_int.h>
#include "rx_cmd.h"
#include "rx_resp.h"

//: Response status codes.  Kept as literals rather than an enum so the generated schema header and
//: this body cannot disagree about the encoding (the schema carries a plain word).
#define RF_SAMP_BUF_OK          0
#define RF_SAMP_BUF_TOO_OLD     1
#define RF_SAMP_BUF_MISALIGNED  2

/// @tparam W       AXIS word width in bits
/// @tparam SPW     samples per word (power of two)
/// @tparam N       buffer depth in WORDS (power of two); the buffer holds N*SPW samples
/// @tparam MARGIN  SAMPLES of horizon given up to bound progress-channel staleness
/// @tparam IDX_W   width of the sample-index counter and of every RxCmd/RxResp field
template <int W, int SPW, int N, int MARGIN, int IDX_W>
static void rf_samp_buf_capture_task(ap_uint<W> buf_r[N], hls::stream<ap_uint<W> >& wr_in,
                                     hls::stream<ap_uint<W> >& s_cmd,
                                     hls::stream<ap_uint<W> >& s_out,
                                     hls::stream<ap_uint<W> >& s_resp) {
    // Survives across commands: what this task last heard about the ingress's position.  Starting at
    // 0 is the honest initial state — "nothing is known to have been written yet".
    static ap_uint<IDX_W> last_wr = 0;

    RxCmd c;
    c.read_stream<W>(s_cmd);             // blocks; the capture side is allowed to

    ap_uint<IDX_W> idx = c.start;
    ap_uint<IDX_W> sent = 0;
    ap_uint<IDX_W> status = RF_SAMP_BUF_OK;

    // Word alignment, decided before anything is emitted.  At SPW == 1 both operands are 0 and the
    // whole branch folds away, which is why this costs nothing in the one-sample-per-word design.
    ap_uint<IDX_W> nword = c.nsamp / SPW;
    if ((c.start % SPW) != 0 || (c.nsamp % SPW) != 0) {
        status = RF_SAMP_BUF_MISALIGNED;
        nword = 0;
    }

    for (ap_uint<IDX_W> i = 0; i < nword; i = i + 1) {
        // -- 1. wait until the word holding sample `idx` has been written ---------------------
        //
        // The circular comparison: `idx - last_wr` interpreted as a SIGNED IDX_W-bit difference is
        // the position of idx relative to the write pointer, and it stays correct across the
        // counter's wrap as long as the two are within 2^(IDX_W-1) of each other -- which the buffer
        // depth and any sane command guarantee.  A plain `idx < last_wr` would break the first time
        // the counter wrapped, and it would break silently.
        bool written = false;
        while (!written) {
            ap_uint<W> w;
            if (wr_in.read_nb(w)) {
                last_wr = (ap_uint<IDX_W>)w;
            }
            ap_int<IDX_W> ahead = (ap_int<IDX_W>)(idx - last_wr);
            written = (ahead < 0);
        }

        // -- 2. horizon: refuse a word that may already have been overwritten -----------------
        ap_int<IDX_W> age = (ap_int<IDX_W>)(last_wr - idx);   // >= 1 here, by the loop above
        if (age > (ap_int<IDX_W>)(N * SPW - MARGIN)) {
            status = RF_SAMP_BUF_TOO_OLD;
            break;                        // no partial nonsense: stop, report, let the host retry
        }

        s_out.write(buf_r[(idx / SPW) & (N - 1)]);
        sent = sent + SPW;
        idx = idx + SPW;
    }

    RxResp r;
    r.tid = c.tid;
    r.status = status;
    r.nsent = sent;
    r.write_stream<W>(s_resp);
}

#endif  // WAVEFLOW_RF_SAMP_BUF_CAPTURE_TASK_H
