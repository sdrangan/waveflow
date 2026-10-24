#ifndef WAVEFLOW_STREAMS_STREAM_BROADCAST_TASK_H
#define WAVEFLOW_STREAMS_STREAM_BROADCAST_TASK_H
// stream_broadcast_task.h -- StreamBroadcast's hls::task body (waveflow/streams/broadcast.py), framework.
//
// ONE FIRING = ONE WORD: read a word, write it to every output, TLAST included.  Nothing here knows a
// message or a packet: the framing travels with the words, so each output carries exactly the input's
// bursts.
//
// Template parameters, in the order the composite generator supplies them (KernelTask: template_args,
// then one word type per `framed` port, in signature order):
//   W   the data width;
//   TI  the input's word type, TO... each output's -- streamutils::axi4s_word<W> at a top's port,
//       streamutils::framed_word<W> on an internal FIFO.  So one body serves the block as the top of its
//       own harness, at a composite's boundary, and deep inside one.
//
// The number of outputs is the number of output ports (a variadic pack): the generator binds ports one
// by one.  Every output is written in the same firing, so a full output stalls them all -- the outputs
// move in lockstep, paced by the slowest consumer.
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"

namespace stream_broadcast_body {

// The end of the recursion: no outputs left.
template <int W>
static inline void put_all(const ap_uint<W>&, bool) {
#pragma HLS INLINE
}

// One output, then the rest.  write_boundary_word sets an axi4s_word's keep / strb; a framed_word has none.
template <int W, typename T0, typename... TR>
static inline void put_all(const ap_uint<W>& data, bool last, hls::stream<T0>& s0, hls::stream<TR>&... rest) {
#pragma HLS INLINE
    streamutils::write_boundary_word<T0, W>(s0, data, last);
    put_all<W>(data, last, rest...);
}

}  // namespace stream_broadcast_body

template <int W, typename TI, typename... TO>
static void stream_broadcast_task(hls::stream<TI>& s_in, hls::stream<TO>&... m_out) {
    const TI x = s_in.read();
    stream_broadcast_body::put_all<W>(ap_uint<W>(x.data), bool(x.last), m_out...);
}

#endif  // WAVEFLOW_STREAMS_STREAM_BROADCAST_TASK_H
