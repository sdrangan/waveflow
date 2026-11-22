#ifndef WAVEFLOW_STREAMS_STREAM_DEMUX_TASK_H
#define WAVEFLOW_STREAMS_STREAM_DEMUX_TASK_H
// stream_demux_task.h -- StreamDemux's hls::task bodies (waveflow/streams/demux.py), framework.
//
// ONE FIRING = ONE FRAME: its output index, then its NB TLAST bursts to that output, word by word.  An
// index that names no output drops the frame (read to its last burst, written nowhere).  Two bodies:
//
//   stream_demux_task<W, NB, TI, TS, TO...>(s_in, s_sel, m_out...)
//       the index is one word on s_sel per frame (an arbiter's m_sel);
//   stream_demux_hdr_task<W, NB, LSB, BITS, TI, TO...>(s_in, m_out...)
//       the index is bits LSB .. LSB+BITS-1 of the frame's first word, which is forwarded with the rest.
//
// Template parameters, in the order the composite generator supplies them (KernelTask: template_args,
// then one word type per `framed` port, in signature order): W the data width; NB TLAST bursts a frame;
// the word types -- streamutils::axi4s_word<W> at a top's port, streamutils::framed_word<W> on an
// internal FIFO.  The outputs are the pack, last in the signature; their number is its length.
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"

namespace stream_demux_body {

// Keep these loops' shape: the count of TLASTs seen, tested at the head (`while (b < NB)`), a narrow
// count (NB <= 255).  Rewrites meant to shorten the path hung at RTL (stream_arbiter_task.h).

// The frame whose first word x0 is already read: x0, then the rest, to d -- a word a cycle.
template <int W, int NB, typename TS, typename TD>
static void copy_frame(const TS& x0, hls::stream<TS>& s, hls::stream<TD>& d) {
    streamutils::write_boundary_word<TD, W>(d, ap_uint<W>(x0.data), bool(x0.last));
    ap_uint<8> b = x0.last ? 1 : 0;
COPY:
    while (b < NB) {
#pragma HLS PIPELINE II=1
        const TS x = s.read();
        streamutils::write_boundary_word<TD, W>(d, ap_uint<W>(x.data), bool(x.last));
        if (x.last) ++b;
    }
}

// The rest of a frame named no output: read to its last burst, written nowhere.
template <int NB, typename TS>
static void drop_frame(const TS& x0, hls::stream<TS>& s) {
    ap_uint<8> b = x0.last ? 1 : 0;
DROP:
    while (b < NB) {
#pragma HLS PIPELINE II=1
        const TS x = s.read();
        if (x.last) ++b;
    }
}

// The frame to output k: the pack walked to its k-th stream; past the end, dropped.
template <int W, int NB, int K, typename TS>
static inline void route(int, const TS& x0, hls::stream<TS>& s) {
#pragma HLS INLINE
    drop_frame<NB>(x0, s);
}

template <int W, int NB, int K, typename TS, typename T0, typename... TR>
static inline void route(int k, const TS& x0, hls::stream<TS>& s, hls::stream<T0>& d0,
                         hls::stream<TR>&... rest) {
#pragma HLS INLINE
    if (k == K) {
        copy_frame<W, NB>(x0, s, d0);
    } else {
        route<W, NB, K + 1>(k, x0, s, rest...);
    }
}

}  // namespace stream_demux_body

template <int W, int NB, typename TI, typename TS, typename... TO>
static void stream_demux_task(hls::stream<TI>& s_in, hls::stream<TS>& s_sel, hls::stream<TO>&... m_out) {
    static_assert(sizeof...(TO) >= 2 && sizeof...(TO) <= 16, "stream_demux_task: 2 to 16 outputs");
    constexpr int N = sizeof...(TO);
    const TS sel = s_sel.read();
    const ap_uint<W> idx = sel.data;
    const int k = idx >= N ? N : int(idx);           // N names no output: dropped
    const TI x0 = s_in.read();
    stream_demux_body::route<W, NB, 0>(k, x0, s_in, m_out...);
}

template <int W, int NB, int LSB, int BITS, typename TI, typename... TO>
static void stream_demux_hdr_task(hls::stream<TI>& s_in, hls::stream<TO>&... m_out) {
    static_assert(sizeof...(TO) >= 2 && sizeof...(TO) <= 16, "stream_demux_hdr_task: 2 to 16 outputs");
    static_assert(BITS >= 1 && BITS <= 16 && LSB + BITS <= W, "stream_demux_hdr_task: the field");
    const TI x0 = s_in.read();
    const int k = int(ap_uint<W>(x0.data).range(LSB + BITS - 1, LSB));
    stream_demux_body::route<W, NB, 0>(k, x0, s_in, m_out...);
}

#endif  // WAVEFLOW_STREAMS_STREAM_DEMUX_TASK_H
