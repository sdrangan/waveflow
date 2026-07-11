#ifndef WAVEFLOW_BUILD_GATHER_TWO_SOB_TASK_H
#define WAVEFLOW_BUILD_GATHER_TWO_SOB_TASK_H
// gather_two_sob_task.h — the FIXED Gather body for the P-SOB interleaver variant: read-lock BOTH
// resident blocks (p_blk index block, x_blk source block) and gather (the symmetric analogue of
// gather_word_task, but with P resident instead of streamed).  A pure-AXIS, single-firing hls::task
// body (the runtime re-fires it per job): per output word read the next index word SEQUENTIALLY from
// p_blk and do LW random elem_read<MEM_DW> reads from x_blk (#pragma HLS UNROLL; LW = MEM_DW/32), then
// pack the LW gathered 32-bit results into one output word.  Copied verbatim by MemStreamStep,
// instantiated <MEM_DW, NW> by the top.  The block element type's array-utils header supplies elem_read.
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
#include "il_elem_array_utils.h"

// p_blk (index words, sequential) x x_blk (source, random) -> y_out: y_word lane l = x_blk[ P[w*LW+l] ].
template <int MEM_DW, int NW>
static void gather_two_sob_task(hls::stream_of_blocks<ap_uint<MEM_DW>[NW] >& p_blk,
                                hls::stream_of_blocks<ap_uint<MEM_DW>[NW] >& x_blk,
                                hls::stream<ap_uint<MEM_DW> >& y_out) {
    const int LW = MEM_DW / 32;
    hls::read_lock<ap_uint<MEM_DW>[NW] > pb(p_blk);
    hls::read_lock<ap_uint<MEM_DW>[NW] > xb(x_blk);
GY: for (int w = 0; w < NW; ++w) {
#pragma HLS PIPELINE II=1
        ap_uint<MEM_DW> pword = pb[w];                               // sequential index word from p_blk
        ap_uint<MEM_DW> yword = 0;
        for (int l = 0; l < LW; ++l) {
#pragma HLS UNROLL
            int idx = (int)pword.range(32 * l + 31, 32 * l);         // output elem index P[w*LW+l]
            ap_uint<32> xv = (ap_uint<32>)il_elem_array_utils::elem_read<MEM_DW>(&xb[0], idx);
            yword.range(32 * l + 31, 32 * l) = xv;                   // pack LW results into one word
        }
        y_out.write(yword);
    }
}

#endif  // WAVEFLOW_BUILD_GATHER_TWO_SOB_TASK_H
