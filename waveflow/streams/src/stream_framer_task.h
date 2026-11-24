#ifndef WAVEFLOW_STREAMS_STREAM_FRAMER_TASK_H
#define WAVEFLOW_STREAMS_STREAM_FRAMER_TASK_H
// stream_framer_task.h -- StreamFramer's hls::task body (waveflow/streams/framer.py), framework.
//
// ONE FIRING = ONE FRAME: N words read from a stream framed by count (no TLAST), the same N written,
// TLAST on the last.  A word a cycle (II = 1); nothing is decided, so the copy is the whole body.
//
// Template parameters, in the order the composite generator supplies them (KernelTask: template_args,
// then one word type per `framed` port, in signature order):
//   W   the data width of both ports;
//   N   the frame length in words (the count the unit before it framed by -- SsrFft's L/R);
//   TO  m_out's word type -- streamutils::axi4s_word<W> at a top's port, streamutils::framed_word<W>
//       on an internal FIFO.  The input is a plain word stream, ap_uint<W>.
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"

template <int W, int N, typename TO>
static void stream_framer_task(hls::stream<ap_uint<W> >& s_in, hls::stream<TO>& m_out) {
    static_assert(N >= 1, "stream_framer_task: a frame is at least one word");
FRAME:
    for (int i = 0; i < N; ++i) {
#pragma HLS PIPELINE II=1
        const ap_uint<W> x = s_in.read();
        streamutils::write_boundary_word<TO, W>(m_out, x, i == N - 1);
    }
}

#endif  // WAVEFLOW_STREAMS_STREAM_FRAMER_TASK_H
