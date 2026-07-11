// interleaver_sob.cpp — GENERATED (examples/interleaver/mem_stream_gen.py).
// DO NOT EDIT: free-running (ap_ctrl_none) hls::task top; the fixed task bodies are the
// validated sandbox a2s/s2a (waveflow/build/mem_*_stream_task.h). Verify via XSI —
// ap_ctrl_none Vitis cosim is unreliable.
#include "hls_task.h"
#include "hls_stream.h"
#include <ap_int.h>
#include "memmgr.hpp"
#include "hls_streamofblocks.h"
#include "il_cmd.h"
#include "m_r_cmd.h"
#include "m_w_cmd.h"
#include "interleaver_seq_task.h"
#include "mem_r_stream_task.h"
#include "split_fill_task.h"
#include "gather_two_sob_task.h"
#include "mem_w_stream_done_task.h"

void interleaver_sob(
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
    hls_thread_local hls::stream<ap_uint<64> > mem_out;
    hls_thread_local hls::stream_of_blocks<ap_uint<64>[128], 2> p_blk;
    hls_thread_local hls::stream_of_blocks<ap_uint<64>[128], 2> x_blk;
    hls_thread_local hls::stream<ap_uint<64> > y_words;
    hls_thread_local hls::task t0(interleaver_seq_task<64, 128>, s_cmd, mr_cmd, mw_cmd);
    hls_thread_local hls::task t1(mem_r_stream_task<64>, mr_cmd, m_in, mem_out);
    hls_thread_local hls::task t2(split_fill_task<64, 128>, mem_out, p_blk, x_blk);
    hls_thread_local hls::task t3(gather_two_sob_task<64, 128>, p_blk, x_blk, y_words);
    hls_thread_local hls::task t4(mem_w_stream_done_task<64>, mw_cmd, y_words, m_out, s_done);
}
