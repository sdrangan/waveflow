#ifndef EXAMPLES_BLOCK_EQ_IO_H
#define EXAMPLES_BLOCK_EQ_IO_H
// block_eq_io.h -- reads and writes on a stream of EITHER word type, for bodies whose port types are
// template parameters (KernelTask.framed): streamutils::axi4s_word<W> at a top's port,
// streamutils::framed_word<W> on an internal FIFO.  Overload resolution picks the generated routine
// for the type, at compile time -- so one body serves a stage as the top of its own harness and inside
// the composite.  (streamutils has write_msg for both types; these add the reads and the float lanes.)
#include "hls_stream.h"
#include "streamutils_hls.h"
#include "float32_array_utils.h"

namespace beq_io {

using streamutils::axi4s_word;
using streamutils::framed_word;

// A schema message.
template <int W, typename Msg>
inline void read_msg(Msg& m, hls::stream<framed_word<W> >& s) { m.template read_framed_stream<W>(s); }
template <int W, typename Msg>
inline void read_msg(Msg& m, hls::stream<axi4s_word<W> >& s) { m.template read_axi4_stream<W>(s); }

// One word of float32 lanes: n is the elements still due (the TLAST bookkeeping of the lane routines).
template <int W>
inline void read_lane(hls::stream<framed_word<W> >& s, float dst[], int n, streamutils::tlast_status& ts) {
#pragma HLS INLINE
    float32_array_utils::read_framed_stream_lane<W>(s, dst, n, ts);
}
template <int W>
inline void read_lane(hls::stream<axi4s_word<W> >& s, float dst[], int n, streamutils::tlast_status& ts) {
#pragma HLS INLINE
    float32_array_utils::read_axi4_stream_lane<W>(s, dst, n, ts);
}
template <int W>
inline void write_lane(const float src[], hls::stream<framed_word<W> >& s, bool tlast, int n) {
#pragma HLS INLINE
    float32_array_utils::write_framed_stream_lane<W>(src, s, tlast, n);
}
template <int W>
inline void write_lane(const float src[], hls::stream<axi4s_word<W> >& s, bool tlast, int n) {
#pragma HLS INLINE
    float32_array_utils::write_axi4_stream_lane<W>(src, s, tlast, n);
}

}  // namespace beq_io

#endif  // EXAMPLES_BLOCK_EQ_IO_H
