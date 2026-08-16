#ifndef WAVEFLOW_RF_SAMP_BUF_INGRESS_TASK_H
#define WAVEFLOW_RF_SAMP_BUF_INGRESS_TASK_H
// rf_samp_buf_ingress_task.h — the RX capture buffer's INGRESS: one sample off the converter port, one
// sample into the buffer, one progress update.  A single firing; the hls::task runtime re-fires it.
//
// THE NEVER-STALL LAW APPLIES TO THIS TASK, AND ONLY TO THIS TASK.
//
// A converter cannot be back-pressured: it presents a beat every sample period and whatever the
// fabric is not ready for is gone (docs/guide/rf/python/fidelity.md, condition 3).  So this body must have
// exactly one blocking call — the `s_in.read()` — and everything after it must be unconditionally
// fast.  The CAPTURE task next door may block for as long as it likes; nothing upstream of it loses
// data when it waits.  Do not copy this law onto that task: it would make the capture wrong.
//
// It satisfies condition 3 STRUCTURALLY, which is stronger than the rf_loopback ingress managed.
// There, the ingress fed an internal FIFO and the argument was about depth: the FIFO had to be deep
// enough to absorb the block stage's busy stretch.  Here the ingress writes a BRAM PORT, and a BRAM
// port cannot back-pressure — there is no `full_n`, no handshake and nothing to size.  The only
// question left is whether the body fits in a sample period, and one read plus one array write does.
//
// THE PROGRESS CHANNEL IS NON-BLOCKING, AND THAT IS THE POINT.
//
// `wr_out.write_nb()` may fail, and a failure is CORRECT rather than tolerated: the value is a
// running position, only the newest one means anything, and a blocking write here would stall the
// converter to deliver a number that is already stale.  The capture's view of `wr` is therefore a
// LOWER BOUND that lags — see rf_samp_buf_capture_task.h, which is where that lag is paid for.
#include "hls_stream.h"
#include <ap_int.h>

/// @tparam W  sample/word width in bits.  One sample per word, so a sample index IS a word index.
/// @tparam N  buffer depth in samples.  MUST be a power of two: the wrap is a bit mask, and a
///            non-power-of-two would need a comparison and a subtract in the never-stall path.
template <int W, int N>
static void rf_samp_buf_ingress_task(ap_uint<W> buf_w[N], hls::stream<ap_uint<W> >& s_in,
                                hls::stream<ap_uint<W> >& wr_out) {
    // The write pointer, in SAMPLE INDEX — free-running and wrapping at 2^W.  It is not the address:
    // the address is the low log2(N) bits of it.  Keeping the index rather than the address is what
    // lets the capture compare positions at all (see the circular comparison next door).
    static ap_uint<W> wr = 0;

    ap_uint<W> x = s_in.read();          // the ONE blocking call in this task
    buf_w[wr & (N - 1)] = x;             // a BRAM port: no handshake, cannot refuse
    wr = wr + 1;
    wr_out.write_nb(wr);                 // may fail; failing is correct (see above)
}

#endif  // WAVEFLOW_RF_SAMP_BUF_INGRESS_TASK_H
