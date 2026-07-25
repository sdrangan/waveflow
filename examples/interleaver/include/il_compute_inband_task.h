#ifndef WAVEFLOW_IL_COMPUTE_INBAND_TASK_H
#define WAVEFLOW_IL_COMPUTE_INBAND_TASK_H
// il_compute_inband_task.h — the in-band interleaver's pure SOB->SOB gather (the CUSTOM compute — the
// kernel the design fits), TYPED-block edition.  Read the framed IlDesc, forward it, then read-lock the
// 32-bit ELEMENT blocks p_blk (indices) + x_blk (values) and write-lock y_blk, and gather:
//
//     y_blk[i] = x_blk[ p_blk[i] ]      // Y[i] = X[P[i]] — laid bare, no lane math
//
// Because il_load already deserialized the words into typed element blocks (ap_uint<32>[N]), the gather
// is a direct 32-bit block-RAM read at a runtime index — no elem_read, no .range(), no packing.  The
// random read x_blk[p_blk[i]] is the whole reason X is landed in a block first.  n runtime (variable
// length); block sized N (the max element count = SOBIF element_type).  Its pysim twin is
// IlComputeInband.run_iter (interleaver_inband.py).
//
// csynth- + XSI-verified.
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "il_desc.h"

template <int MEM_DW, int N>
static void il_compute_inband_task(hls::stream<streamutils::framed_word<MEM_DW> >& desc_in,
                                   hls::stream_of_blocks<ap_uint<32>[N] >& p_blk,
                                   hls::stream_of_blocks<ap_uint<32>[N] >& x_blk,
                                   hls::stream<streamutils::framed_word<MEM_DW> >& desc_out,
                                   hls::stream_of_blocks<ap_uint<32>[N] >& y_blk) {
    IlDesc d;
    streamutils::tlast_status tl;
    d.read_framed_stream<MEM_DW>(desc_in, tl);
    d.write_framed_stream<MEM_DW>(desc_out);        // forward the descriptor
    const int n = (int)d.n;
    hls::read_lock<ap_uint<32>[N] > pb(p_blk);
    hls::read_lock<ap_uint<32>[N] > xb(x_blk);
    hls::write_lock<ap_uint<32>[N] > yb(y_blk);
CY: for (int i = 0; i < n; ++i) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT max=N
        yb[i] = xb[pb[i]];                          // the gather: Y[i] = X[P[i]]
    }
}

#endif  // WAVEFLOW_IL_COMPUTE_INBAND_TASK_H
