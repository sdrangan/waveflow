// mem_copy.cpp — GENERATED (examples/interleaver/mem_stream_gen.py).
// DO NOT EDIT: free-running (ap_ctrl_none) hls::task top; the fixed task bodies are the
// validated sandbox a2s/s2a (waveflow/build/mem_*_stream_task.h). Verify via XSI —
// ap_ctrl_none Vitis cosim is unreliable.
#include "hls_task.h"
#include "hls_stream.h"
#include <ap_int.h>
#include "memmgr.hpp"
#include "copy_cmd.h"
#include "m_r_cmd.h"
#include "m_w_cmd.h"
#include "mem_complete.h"
#include "u_int32_array.h"
#include "mem_seq_task.h"
#include "mem_r_stream_task.h"
#include "mem_w_stream_done_task.h"

void mem_copy(
    hls::stream<ap_uint<64> >& s_cmd,
    const ap_uint<64>* m_in,
    ap_uint<64>* m_out,
    hls::stream<ap_uint<64> >& s_done
) {
#pragma HLS INTERFACE axis port=s_cmd
#pragma HLS INTERFACE m_axi port=m_in offset=slave bundle=gmem0 depth=8192
#pragma HLS stable variable=m_in
#pragma HLS INTERFACE m_axi port=m_out offset=slave bundle=gmem1 depth=8192
#pragma HLS INTERFACE axis port=s_done
#pragma HLS INTERFACE ap_ctrl_none port=return
    hls_thread_local hls::stream<ap_uint<64> > mr_cmd;
    hls_thread_local hls::stream<ap_uint<64> > mw_cmd;
    hls_thread_local hls::stream<ap_uint<64> > copy_data;
    hls_thread_local hls::task t0(mem_seq_task<64>, s_cmd, mr_cmd, mw_cmd);
    hls_thread_local hls::task t1(mem_r_stream_task<64>, mr_cmd, m_in, copy_data);
    hls_thread_local hls::task t2(mem_w_stream_done_task<64>, mw_cmd, copy_data, m_out, s_done);
}
