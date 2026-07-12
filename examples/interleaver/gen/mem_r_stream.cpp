// mem_r_stream.cpp — GENERATED (examples/interleaver/mem_stream_gen.py).
// DO NOT EDIT: free-running (ap_ctrl_none) hls::task top; the fixed task bodies are the
// validated sandbox a2s/s2a (waveflow/build/mem_*_stream_task.h). Verify via XSI —
// ap_ctrl_none Vitis cosim is unreliable.
#include "hls_task.h"
#include "hls_stream.h"
#include <ap_int.h>
#include "memmgr.hpp"
#include "m_r_cmd.h"
#include "mem_r_stream_task.h"

void mem_r_stream(
    hls::stream<ap_uint<64> >& s_cmd,
    const ap_uint<64>* m_mem,
    hls::stream<ap_uint<64> >& m_out
) {
#pragma HLS INTERFACE axis port=s_cmd
#pragma HLS INTERFACE m_axi port=m_mem offset=slave bundle=gmem0 depth=8192
#pragma HLS stable variable=m_mem
#pragma HLS INTERFACE axis port=m_out
#pragma HLS INTERFACE ap_ctrl_none port=return
    hls_thread_local hls::task t0(mem_r_stream_task<64>, s_cmd, m_mem, m_out);
}
