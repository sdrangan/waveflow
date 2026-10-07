// wf_matrix_io.h -- a matrix between a framed message burst and lane-group blocks.
//
// A matrix travels row-major as memory elements (complex, lane_bits per part, the register's
// integer bits), as many to a word as fit, through the generated array utils (MEM is a memory
// element struct of a component's traits: waveflow/linalg/formats.py).  In a block it is held as
// lane groups (wf_lanes.h).  The memory <-> register conversion is an ap_fixed assignment, exact
// because the memory format has the register's integer bits and at least its fraction bits.
// The element count n is a run-time value, at most MAXG * L, and L is a power of two; a last
// group that n does not fill is written with zeros in its other lanes.  wf_load_matrix_transposed
// writes the transpose of what it reads, a value per cycle at best (a lane-group read-modify-write).
// Python twin: waveflow/linalg/lanes.py (to_words, from_words).
#ifndef WF_MATRIX_IO_H
#define WF_MATRIX_IO_H
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "wf_lanes.h"

// Deserialize the n elements of one burst into lane groups of register type T.  Every lane is fixed
// wiring: when a group spans words (L >= LW), each word's values enter a group register from the
// top, a full group is written, and a last group the burst does not fill is shifted into place
// after the loop; when a word holds several groups, each group is taken from fixed lanes.
template <int WBW, int L, int MAXG, class T, class MEM>
static void wf_load_matrix(hls::stream<streamutils::framed_word<WBW> >& s_in,
                           typename wf_lanes::group<T, L>::type blk[MAXG], int n,
                           streamutils::tlast_status& tl) {
#pragma HLS INLINE
    static_assert((L & (L - 1)) == 0, "the lane count must be a power of two");
    typedef typename wf_lanes::group<T, L>::type grp_t;
    const int LW = MEM::template lane_capacity<WBW>();
    const int VB = 2 * T::width;  // bits of one value in a group
    const int NW = (n + LW - 1) / LW;
    if (L >= LW) {
        const int WPG = L / LW;  // words per group
        grp_t grp = 0;
        int q = 0, g = 0, e0 = 0;  // words in the group so far, the group, the word's first value
    WORDS:
        for (int w = 0; w < NW; ++w) {
#pragma HLS LOOP_TRIPCOUNT max = (MAXG * L + LW - 1) / LW
#pragma HLS PIPELINE II = 1
            typename MEM::value_type buf[LW];
            MEM::template read_framed_stream_lane<WBW>(s_in, buf, LW, tl);
            grp_t in = 0;
            for (int i = 0; i < LW; ++i) {
#pragma HLS UNROLL
                T re = 0, im = 0;
                if (e0 + i < n) {
                    re = buf[i].real();
                    im = buf[i].imag();
                }
                wf_lanes::set<T, L>(in, L - LW + i, re, im);
            }
            grp = (grp >> (LW * VB)) | in;
            if (q == WPG - 1) {
                blk[g] = grp;
                ++g;
                q = 0;
            } else {
                ++q;
            }
            e0 += LW;
        }
        if (q != 0) blk[g] = grp >> ((WPG - q) * LW * VB);
    } else {
        const int GPW = LW / L;  // groups per word
        int e0 = 0;
    WORDS_G:
        for (int w = 0; w < NW; ++w) {
#pragma HLS LOOP_TRIPCOUNT max = (MAXG * L + LW - 1) / LW
#pragma HLS PIPELINE II = 1
            typename MEM::value_type buf[LW];
            MEM::template read_framed_stream_lane<WBW>(s_in, buf, LW, tl);
            for (int j = 0; j < GPW; ++j) {
#pragma HLS UNROLL
                grp_t gj = 0;
                for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                    T re = 0, im = 0;
                    if (e0 + j * L + l < n) {
                        re = buf[j * L + l].real();
                        im = buf[j * L + l].imag();
                    }
                    wf_lanes::set<T, L>(gj, l, re, im);
                }
                if (e0 + j * L < n) blk[w * GPW + j] = gj;
            }
            e0 += LW;
        }
    }
}

// Deserialize a rows x cols matrix (row-major, one burst) into the lane groups of its transpose
// (cols x rows): stored value (r, c) goes to lane (c rows + r) % L of group (c rows + r) / L.
template <int WBW, int L, int MAXG, class T, class MEM>
static void wf_load_matrix_transposed(hls::stream<streamutils::framed_word<WBW> >& s_in,
                                      typename wf_lanes::group<T, L>::type blk[MAXG], int rows,
                                      int cols, streamutils::tlast_status& tl) {
#pragma HLS INLINE
    static_assert((L & (L - 1)) == 0, "the lane count must be a power of two");
    const int LW = MEM::template lane_capacity<WBW>();
    typename MEM::value_type buf[LW];
    int i = 0;           // the value's lane in its word
    int c = 0, r = 0;    // the value (r, c)
    int x = 0;           // its index in the transpose, c rows + r
ELEMS:
    for (int e = 0; e < rows * cols; ++e) {
#pragma HLS LOOP_TRIPCOUNT max = MAXG * L
#pragma HLS PIPELINE
        if (i == 0) MEM::template read_framed_stream_lane<WBW>(s_in, buf, LW, tl);
        T re = buf[i].real(), im = buf[i].imag();
        typename wf_lanes::group<T, L>::type g = blk[x / L];
        wf_lanes::set<T, L>(g, x % L, re, im);
        blk[x / L] = g;
        i = (i == LW - 1) ? 0 : i + 1;
        if (c == cols - 1) {
            c = 0;
            ++r;
            x = r;
        } else {
            ++c;
            x += rows;
        }
    }
}

// Serialize n elements of lane groups of register type T as one burst (last on its final word),
// with zeros in the lanes of the final word past n.  The mirror of wf_load_matrix: values leave a
// group register from the bottom, or a word takes several groups from fixed lanes.
template <int WBW, int L, int MAXG, class T, class MEM>
static void wf_store_matrix(const typename wf_lanes::group<T, L>::type blk[MAXG],
                            hls::stream<streamutils::framed_word<WBW> >& s_out, int n) {
#pragma HLS INLINE
    static_assert((L & (L - 1)) == 0, "the lane count must be a power of two");
    typedef typename wf_lanes::group<T, L>::type grp_t;
    const int LW = MEM::template lane_capacity<WBW>();
    const int VB = 2 * T::width;
    const int NW = (n + LW - 1) / LW;
    if (L >= LW) {
        const int WPG = L / LW;
        grp_t cur = 0;
        int q = 0, g = 0, e0 = 0;
    WORDS:
        for (int w = 0; w < NW; ++w) {
#pragma HLS LOOP_TRIPCOUNT max = (MAXG * L + LW - 1) / LW
#pragma HLS PIPELINE II = 1
            if (q == 0) cur = blk[g];
            typename MEM::value_type buf[LW];
            for (int i = 0; i < LW; ++i) {
#pragma HLS UNROLL
                T re, im;
                wf_lanes::get<T, L>(cur, i, re, im);
                if (e0 + i >= n) {
                    re = 0;
                    im = 0;
                }
                buf[i] = typename MEM::value_type(re, im);
            }
            MEM::template write_framed_stream_lane<WBW>(buf, s_out, w == NW - 1, LW);
            cur >>= LW * VB;
            if (q == WPG - 1) {
                q = 0;
                ++g;
            } else {
                ++q;
            }
            e0 += LW;
        }
    } else {
        const int GPW = LW / L;
        int e0 = 0;
    WORDS_G:
        for (int w = 0; w < NW; ++w) {
#pragma HLS LOOP_TRIPCOUNT max = (MAXG * L + LW - 1) / LW
#pragma HLS PIPELINE II = 1
            typename MEM::value_type buf[LW];
            for (int j = 0; j < GPW; ++j) {
#pragma HLS UNROLL
                const grp_t gj = (e0 + j * L < n) ? blk[w * GPW + j] : grp_t(0);
                for (int l = 0; l < L; ++l) {
#pragma HLS UNROLL
                    T re, im;
                    wf_lanes::get<T, L>(gj, l, re, im);
                    if (e0 + j * L + l >= n) {
                        re = 0;
                        im = 0;
                    }
                    buf[j * L + l] = typename MEM::value_type(re, im);
                }
            }
            MEM::template write_framed_stream_lane<WBW>(buf, s_out, w == NW - 1, LW);
            e0 += LW;
        }
    }
}

#endif  // WF_MATRIX_IO_H
