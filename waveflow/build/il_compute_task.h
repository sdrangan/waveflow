#ifndef WAVEFLOW_BUILD_IL_COMPUTE_TASK_H
#define WAVEFLOW_BUILD_IL_COMPUTE_TASK_H
// il_compute_task.h — stage 4 of the canonical six-stage interleaver: the pure SOB->SOB compute (no
// m_axi, no streams besides the token).  Read the per-job token, forward it, then read-lock p_blk +
// x_blk and write-lock y_blk; per output word read the index word SEQUENTIALLY from p_blk and do LW
// random elem_read<MEM_DW> reads from x_blk (#pragma HLS UNROLL; LW = MEM_DW/32), packing the LW
// gathered 32-bit results into one y_blk word (Y[i] = X[P[i]]).  Copied by MemStreamStep,
// instantiated <MEM_DW, NW>.  The block element type's array-utils header supplies elem_read.
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
#include "il_cmd.h"
#include "il_elem_array_utils.h"

template <int MEM_DW, int NW>
static void il_compute_task(hls::stream<ap_uint<MEM_DW> >& cmd_in,
                            hls::stream_of_blocks<ap_uint<MEM_DW>[NW] >& p_blk,
                            hls::stream_of_blocks<ap_uint<MEM_DW>[NW] >& x_blk,
                            hls::stream<ap_uint<MEM_DW> >& cmd_out,
                            hls::stream_of_blocks<ap_uint<MEM_DW>[NW] >& y_blk) {
    const int LW = MEM_DW / 32;
    InterleaverCmd c;
    c.read_stream<MEM_DW>(cmd_in);
    c.write_stream<MEM_DW>(cmd_out);        // forward the token
    hls::read_lock<ap_uint<MEM_DW>[NW] > pb(p_blk);
    hls::read_lock<ap_uint<MEM_DW>[NW] > xb(x_blk);
    hls::write_lock<ap_uint<MEM_DW>[NW] > yb(y_blk);
CY: for (int w = 0; w < NW; ++w) {
#pragma HLS PIPELINE II=1
        ap_uint<MEM_DW> pword = pb[w];                              // sequential index word from p_blk
        ap_uint<MEM_DW> yword = 0;
        for (int l = 0; l < LW; ++l) {
#pragma HLS UNROLL
            int idx = (int)pword.range(32 * l + 31, 32 * l);        // output elem index P[w*LW+l]
            ap_uint<32> xv = (ap_uint<32>)il_elem_array_utils::elem_read<MEM_DW>(&xb[0], idx);
            yword.range(32 * l + 31, 32 * l) = xv;                  // pack LW results into one word
        }
        yb[w] = yword;
    }
}

#endif  // WAVEFLOW_BUILD_IL_COMPUTE_TASK_H
