// wf_matrix_io.h -- a matrix between a framed message burst and lane-group blocks.
//
// A matrix travels row-major as memory elements (complex, lane_bits per part, the register's
// integer bits), as many to a word as fit, through the generated array utils (MEM is a memory
// element struct of a component's traits: waveflow/linalg/formats.py).  In a block it is held as
// lane groups (wf_lanes.h).  The memory <-> register conversion is an ap_fixed assignment, exact
// because the memory format has the register's integer bits and at least its fraction bits.
// The element count n is a run-time value, at most MAXG * L; it is a multiple of L, and L is a
// power of two.  Python twin: waveflow/linalg/lanes.py (to_words, from_words).
#ifndef WF_MATRIX_IO_H
#define WF_MATRIX_IO_H
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "wf_lanes.h"

// Deserialize the n elements of one burst into lane groups of register type T.
template <int WBW, int L, int MAXG, class T, class MEM>
static void wf_load_matrix(hls::stream<streamutils::framed_word<WBW> >& s_in,
                           typename wf_lanes::group<T, L>::type blk[MAXG], int n,
                           streamutils::tlast_status& tl) {
#pragma HLS INLINE
    static_assert((L & (L - 1)) == 0, "the lane count must be a power of two");
    const int LW = MEM::template lane_capacity<WBW>();
    const int NW = (n + LW - 1) / LW;
    typename wf_lanes::group<T, L>::type grp = 0;
WORDS:
    for (int w = 0; w < NW; ++w) {
#pragma HLS LOOP_TRIPCOUNT max = (MAXG * L + LW - 1) / LW
#pragma HLS PIPELINE II = 1
        typename MEM::value_type buf[LW];
        MEM::template read_framed_stream_lane<WBW>(s_in, buf, LW, tl);
        for (int i = 0; i < LW; ++i) {
#pragma HLS UNROLL
            const int e = w * LW + i;
            if (e < n) {
                T re = buf[i].real(), im = buf[i].imag();
                wf_lanes::set<T, L>(grp, e % L, re, im);
                if (e % L == L - 1) blk[e / L] = grp;
            }
        }
    }
}

// Serialize n elements of lane groups of register type T as one burst (last on its final word).
template <int WBW, int L, int MAXG, class T, class MEM>
static void wf_store_matrix(const typename wf_lanes::group<T, L>::type blk[MAXG],
                            hls::stream<streamutils::framed_word<WBW> >& s_out, int n) {
#pragma HLS INLINE
    static_assert((L & (L - 1)) == 0, "the lane count must be a power of two");
    const int LW = MEM::template lane_capacity<WBW>();
    const int NW = (n + LW - 1) / LW;
WORDS:
    for (int w = 0; w < NW; ++w) {
#pragma HLS LOOP_TRIPCOUNT max = (MAXG * L + LW - 1) / LW
#pragma HLS PIPELINE II = 1
        typename MEM::value_type buf[LW];
        for (int i = 0; i < LW; ++i) {
#pragma HLS UNROLL
            const int e = w * LW + i;
            T re = 0, im = 0;
            if (e < n) wf_lanes::get<T, L>(blk[e / L], e % L, re, im);
            buf[i] = typename MEM::value_type(re, im);
        }
        MEM::template write_framed_stream_lane<WBW>(buf, s_out, w == NW - 1, LW);
    }
}

#endif  // WF_MATRIX_IO_H
