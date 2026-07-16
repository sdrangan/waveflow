// gather_toy_top.cpp — gather_toy top: free-running (ap_ctrl_none) hls::task pair proving the
// typed SOBIF ping-pong lowers and synthesizes.
//
//   Fill (stream->SOB) -> stream_of_blocks depth 2 -> Gather (SOB->stream)
//
// Pure-AXIS by construction: an ap_ctrl_none free-running region cannot carry m_axi or s_axilite
// (Vitis 2025.1), which is exactly why the toy stays stream-only.  Structure mirrors the
// XSI-verified interleaver_canon.cpp top.
//
// MEM_DW=64: 64-bit words.  BLOCK_N=8: 8 words per block (512-bit block).
#include "hls_task.h"
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
#include "fill.h"
#include "gather.h"

static const int MEM_DW = 64;
static const int BLOCK_N = 8;

void gather_toy_top(
    hls::stream<ap_uint<MEM_DW> >& s_in,
    hls::stream<ap_uint<MEM_DW> >& m_out
) {
#pragma HLS INTERFACE axis port=s_in
#pragma HLS INTERFACE axis port=m_out
#pragma HLS INTERFACE ap_ctrl_none port=return
    hls_thread_local hls::stream_of_blocks<ap_uint<MEM_DW>[BLOCK_N], 2> sob;
    hls_thread_local hls::task t0(fill<MEM_DW, BLOCK_N>, s_in, sob);
    hls_thread_local hls::task t1(gather<MEM_DW, BLOCK_N>, sob, m_out);
}
