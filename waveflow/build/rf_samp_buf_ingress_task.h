#ifndef WAVEFLOW_RF_SAMP_BUF_INGRESS_TASK_H
#define WAVEFLOW_RF_SAMP_BUF_INGRESS_TASK_H
// rf_samp_buf_ingress_task.h — the RX sample buffer's INGRESS: one WORD off the converter port, one
// word into the buffer, one progress update.  A single firing; the hls::task runtime re-fires it.
//
// THE NEVER-STALL LAW APPLIES TO THIS TASK, AND ONLY TO THIS TASK.
//
// A converter cannot be back-pressured: it presents a beat every sample period and whatever the
// fabric is not ready for is gone (docs/guide/rf/python/fidelity.md, condition 3).  So this body must
// have exactly one blocking call — the `s_in.read()` — and everything after it must be
// unconditionally fast.  The CAPTURE task next door may block for as long as it likes; nothing
// upstream of it loses data when it waits.  Do not copy this law onto that task: it would make the
// capture wrong.
//
// It satisfies condition 3 STRUCTURALLY, which is stronger than the rf_loopback ingress managed.
// There, the ingress fed an internal FIFO and the argument was about depth: the FIFO had to be deep
// enough to absorb the block stage's busy stretch.  Here the ingress writes a BRAM PORT, and a BRAM
// port cannot back-pressure — there is no `full_n`, no handshake and nothing to size.  The only
// question left is whether the body fits in a sample period, and one read plus one array write does.
//
// A WORD CARRIES SPW SAMPLES, AND THAT IS THE THROUGHPUT LEVER.
//
// One firing moves one WORD, so the sample rate this stage absorbs is SPW / fire_cycles per fabric
// cycle — 0.5 samples/cycle at one sample per word, 2.0 at four.  Widening the word is how a design
// keeps up with a real converter; making this body cheaper (fire_cycles -> 1) is the other half and
// is separate work.  The buffer is therefore N WORDS deep and holds N*SPW samples.
//
// THE PROGRESS CHANNEL IS NON-BLOCKING, AND THAT IS THE POINT.
//
// `wr_out.write_nb()` may fail, and a failure is CORRECT rather than tolerated: the value is a
// running position, only the newest one means anything, and a blocking write here would stall the
// converter to deliver a number that is already stale.  The capture's view of `wr` is therefore a
// LOWER BOUND that lags — see rf_samp_buf_capture_task.h, which is where that lag is paid for.
#include "hls_stream.h"
#include <ap_int.h>

/// @tparam W      AXIS word width in bits — SPW samples of W/SPW bits each.
/// @tparam SPW    samples per word.  MUST be a power of two: the sample->word conversion below is
///                then a shift, and a divide in the never-stall path costs cycles the converter does
///                not give back.
/// @tparam N      buffer depth in WORDS.  MUST be a power of two: the wrap is a bit mask.
/// @tparam IDX_W  width of the SAMPLE-INDEX counter, and of every RxCmd/RxResp field.  Deliberately
///                independent of W: widening the word must not move where the sample counter wraps,
///                or a command naming a sample would mean different things at different widths.
template <int W, int SPW, int N, int IDX_W>
static void rf_samp_buf_ingress_task(ap_uint<W> buf_w[N], hls::stream<ap_uint<W> >& s_in,
                                     hls::stream<ap_uint<W> >& wr_out) {
    // The write pointer, in SAMPLE INDEX — free-running and wrapping at 2^IDX_W.  It is not the
    // address: the address is the low log2(N) bits of `wr / SPW`.  Keeping the index rather than the
    // address is what lets the capture compare positions at all (see the circular comparison next
    // door), and keeping it in SAMPLES rather than words is what lets a host name a sample.
    static ap_uint<IDX_W> wr = 0;

    // RESET-QUALIFIED.  A static's `= 0` is a simulation initial value, not a reset, and the update
    // carries no ap_rst term.  This body happens to be held still during reset by its blocking
    // s_in.read(), but that is a property of what is upstream, not of this task -- and the same
    // omission cost `examples/rf_blk_delay` a day on a body that had nothing to block it.
#pragma HLS reset variable=wr

    // ONE WORD PER CYCLE, FOREVER, and here the loop buys something the player's does not: **the
    // task never stops reading**.  A firing boundary -- or a bounded loop's -- is a window in which
    // this task is not draining the port, and an ADC cannot be told to wait, so words arriving in
    // that window are lost.  Measured in `plans/witness/task_loop/`: the bounded shapes show a
    // 3-cycle boundary gap and `while (1)` shows ZERO.  At today's converter rate the 2-deep
    // boundary port absorbs the gap and nothing is lost; at port capacity it would not.
    while (1) {
#pragma HLS PIPELINE II=1
        ap_uint<W> x = s_in.read();               // the ONE blocking call in this task
        buf_w[(wr / SPW) & (N - 1)] = x;          // a BRAM port: no handshake, cannot refuse
        wr = wr + SPW;
        wr_out.write_nb((ap_uint<W>)wr);          // may fail; failing is correct (see above)
    }
}

#endif  // WAVEFLOW_RF_SAMP_BUF_INGRESS_TASK_H
