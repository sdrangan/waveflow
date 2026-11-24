// A minimal stand-in for waveflow/build/streamutils_hls.h: the two word types and write_boundary_word.
#pragma once
#include "hls_stream.h"
#include <ap_int.h>
namespace streamutils {
template <int W> struct framed_word { ap_uint<W> data; ap_uint<1> last; };
template <int W> struct axi4s_word { ap_uint<W> data; ap_uint<1> last; };
template <typename WordT, int W> inline void write_boundary_word(hls::stream<WordT>& s, ap_uint<W> data, bool last) {
    WordT w; w.data = data; w.last = last; s.write(w);
}
}  // namespace streamutils
