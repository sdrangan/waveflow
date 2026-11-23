// A minimal stand-in for waveflow/build/streamutils_hls.h: what os_pre_task.h uses -- the framed word,
// a word read and written, a one-word message read and written -- for a g++ check of its logic only
// (tests/examples/test_os_filterbank.py).  Not Vitis csim, and nothing about timing or synthesis.
#pragma once
#include "hls_stream.h"
#include <ap_int.h>
namespace streamutils {
template <int W> struct framed_word { ap_uint<W> data; ap_uint<1> last; };
template <typename WordT, int W> inline void write_boundary_word(hls::stream<WordT>& s, ap_uint<W> data, bool last) {
    WordT w; w.data = data; w.last = last; s.write(w);
}
template <typename WordT, int W> inline ap_uint<W> read_boundary_word(hls::stream<WordT>& s, bool& last) {
    WordT w = s.read(); last = bool(w.last.x); return w.data;
}
// A one-word message (the stand-in Tag): its word, TLAST set.
template <int W, typename Msg> inline void read_msg(Msg& m, hls::stream<framed_word<W> >& s) {
    bool last; m.word = read_boundary_word<framed_word<W>, W>(s, last);
}
template <int W, typename Msg> inline void write_msg(const Msg& m, hls::stream<framed_word<W> >& s) {
    write_boundary_word<framed_word<W>, W>(s, m.word, true);
}
}  // namespace streamutils
