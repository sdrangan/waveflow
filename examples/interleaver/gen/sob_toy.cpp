// sob_toy.cpp — GENERATED (examples/interleaver/mem_stream_gen.py).
// DO NOT EDIT: free-running (ap_ctrl_none) hls::task top; the fixed task bodies are the
// validated sandbox a2s/s2a (waveflow/build/mem_*_stream_task.h). Verify via XSI —
// ap_ctrl_none Vitis cosim is unreliable.
#include "hls_task.h"
#include "hls_stream.h"
#include <ap_int.h>
#include "memmgr.hpp"
#include "hls_streamofblocks.h"
#include "fill_task.h"
#include "gather_task.h"

void sob_toy(
    hls::stream<ap_uint<32> >& x_in,
    hls::stream<ap_uint<32> >& p_in,
    hls::stream<ap_uint<32> >& y_out
) {
#pragma HLS INTERFACE axis port=x_in
#pragma HLS INTERFACE axis port=p_in
#pragma HLS INTERFACE axis port=y_out
#pragma HLS INTERFACE ap_ctrl_none port=return
    hls_thread_local hls::stream_of_blocks<ap_uint<32>[256], 2> x_blk;
    hls_thread_local hls::task t0(fill_task<32, 256>, x_in, x_blk);
    hls_thread_local hls::task t1(gather_task<32, 256>, p_in, x_blk, y_out);
}
