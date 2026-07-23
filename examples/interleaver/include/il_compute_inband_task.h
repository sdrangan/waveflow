#ifndef WAVEFLOW_IL_COMPUTE_INBAND_TASK_H
#define WAVEFLOW_IL_COMPUTE_INBAND_TASK_H
// il_compute_inband_task.h — the in-band interleaver's pure SOB->SOB gather (the CUSTOM compute — the
// kernel the design fits).  Read the framed IlDesc, forward it, then read-lock p_blk + x_blk and
// write-lock y_blk; per output word read the index word SEQUENTIALLY from p_blk and do LW random
// elem_read<MEM_DW> reads from x_blk (#pragma HLS UNROLL; LW = MEM_DW/32), packing the LW gathered
// 32-bit results into one y_blk word (Y[i] = X[P[i]]).  nw = ceil(n/LW) is RUNTIME (variable length);
// the block is sized NW.  Its pysim twin is IlComputeInband.run_iter (interleaver_inband.py).
//
// !! UNVERIFIED — pending a csynth/XSI run.  Identical gather to the verified il_compute_task.h, but the
// !! descriptor is the framed IlDesc (not the plain token) and the loop bound is runtime nw.
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "il_desc.h"
#include "il_elem_array_utils.h"

template <int MEM_DW, int NW>
static void il_compute_inband_task(hls::stream<streamutils::framed_word<MEM_DW> >& desc_in,
                                   hls::stream_of_blocks<ap_uint<MEM_DW>[NW] >& p_blk,
                                   hls::stream_of_blocks<ap_uint<MEM_DW>[NW] >& x_blk,
                                   hls::stream<streamutils::framed_word<MEM_DW> >& desc_out,
                                   hls::stream_of_blocks<ap_uint<MEM_DW>[NW] >& y_blk) {
    const int LW = MEM_DW / 32;
    IlDesc d;
    streamutils::tlast_status tl;
    d.read_framed_stream<MEM_DW>(desc_in, tl);
    d.write_framed_stream<MEM_DW>(desc_out);        // forward the descriptor
    const int nw = ((int)d.n + LW - 1) / LW;
    hls::read_lock<ap_uint<MEM_DW>[NW] > pb(p_blk);
    hls::read_lock<ap_uint<MEM_DW>[NW] > xb(x_blk);
    hls::write_lock<ap_uint<MEM_DW>[NW] > yb(y_blk);
CY: for (int w = 0; w < nw; ++w) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT max=NW
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

#endif  // WAVEFLOW_IL_COMPUTE_INBAND_TASK_H
