// cg_io.h -- moving a register matrix between a framed memory stream and lane-group blocks.
//
// Shared by the per-block composites' load and store stages.  A matrix travels in memory format
// (16-bit, the register's integer bits) through the generated array utils (read_/write_framed_
// stream_lane, via the cg::*_mem structs) and lives in a block as lane groups (cg_lanes.h).  The
// memory <-> register conversion is an ap_fixed assignment, exact because memory values come from that
// register format.  Elements are row-major, so lane groups complete in order and each is written once.
#ifndef MIMO_CG_IO_H
#define MIMO_CG_IO_H
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "cg_iter_cmd.h"
#include "cg_types.h"
#include "cg_lanes.h"

// Deserialize one K x N burst into lane groups of register type T.
template <int MEM_DW, int K, int N, int L, class T, class MEM>
static void cg_load_matrix(hls::stream<streamutils::framed_word<MEM_DW> >& s_in,
                           typename cg_lanes::group<T, L>::type blk[K * N / L],
                           streamutils::tlast_status& tl) {
#pragma HLS INLINE
    const int LW = MEM::template lane_capacity<MEM_DW>();
    const int NW = (K * N + LW - 1) / LW;
    typename cg_lanes::group<T, L>::type grp = 0;
WORDS:
    for (int w = 0; w < NW; ++w) {
#pragma HLS PIPELINE II=1
        typename MEM::value_type buf[LW];
        MEM::template read_framed_stream_lane<MEM_DW>(s_in, buf, LW, tl);
        for (int i = 0; i < LW; ++i) {
#pragma HLS UNROLL
            const int e = w * LW + i;
            if (e < K * N) {
                T re = buf[i].real(), im = buf[i].imag();
                cg_lanes::set<T, L>(grp, e % L, re, im);
                if (e % L == L - 1) blk[e / L] = grp;
            }
        }
    }
}

template <int MEM_DW>
static void cg_send(hls::stream<streamutils::framed_word<MEM_DW> >& cmd_out, IterOp op, int it) {
#pragma HLS INLINE
    CgIterCmd c;
    c.op = op;
    c.it = it;
    c.write_framed_stream<MEM_DW>(cmd_out);
}

// Serialize one K x N block of register type T as one data frame.
template <int MEM_DW, int K, int N, int L, class T, class MEM>
static void cg_store_matrix(const typename cg_lanes::group<T, L>::type blk[K * N / L],
                            hls::stream<streamutils::framed_word<MEM_DW> >& cmd_out) {
#pragma HLS INLINE
    const int LW = MEM::template lane_capacity<MEM_DW>();
    const int NW = (K * N + LW - 1) / LW;
WORDS:
    for (int w = 0; w < NW; ++w) {
#pragma HLS PIPELINE II=1
        typename MEM::value_type buf[LW];
        for (int i = 0; i < LW; ++i) {
#pragma HLS UNROLL
            const int e = w * LW + i;
            T re = 0, im = 0;
            if (e < K * N) cg_lanes::get<T, L>(blk[e / L], e % L, re, im);
            buf[i] = typename MEM::value_type(re, im);
        }
        MEM::template write_framed_stream_lane<MEM_DW>(buf, cmd_out, w == NW - 1, LW);
    }
}

#endif  // MIMO_CG_IO_H
