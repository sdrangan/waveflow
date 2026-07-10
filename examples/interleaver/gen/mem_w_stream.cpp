// mem_w_stream.cpp — GENERATED (template codegen, examples/interleaver/mem_stream_gen.py).
// DO NOT EDIT: the fixed body is the validated sandbox stream->AXI (s2a) (interleaver_task_sob3.cpp).
//
// Free-running (ap_ctrl_none) single-hls::task memory endpoint, word-granular (ap_uint<MEM_DW>).
// The sole m_axi write owner touches ONLY streams (the DTLP /
// hls::task+m_axi de-risk: an m_axi owner never also locks a stream_of_blocks). Verify via XSI —
// ap_ctrl_none Vitis cosim is unreliable.
#include "hls_task.h"
#include "hls_stream.h"
#include <ap_int.h>
#include "memmgr.hpp"
#include "m_w_cmd.h"

#ifndef MEM_DW
#define MEM_DW 64
#endif
typedef ap_uint<MEM_DW> word_t;

// stream->AXI (s2a): dequeue one MWCmd, byte_addr -> word index, burst n_words words. Word rate.
static void mem_w_stream_task(hls::stream<word_t>& s_cmd,
                              hls::stream<word_t>& s_in,
                              ap_uint<MEM_DW>* m_mem) {
    MWCmd c;
    c.read_stream<MEM_DW>(s_cmd);
    const int w0 = waveflow::memmgr::byte_addr_to_word_index<MEM_DW>(c.byte_addr);
    const int nw = (int)c.n_words;
S2A: for (int w = 0; w < nw; ++w) {
#pragma HLS PIPELINE II=1
        m_mem[w0 + w] = s_in.read();
    }
}

void mem_w_stream(hls::stream<word_t>& s_cmd,
                  hls::stream<word_t>& s_in,
                  ap_uint<MEM_DW>* m_mem) {
#pragma HLS INTERFACE axis port=s_cmd
#pragma HLS INTERFACE axis port=s_in
#pragma HLS INTERFACE m_axi port=m_mem offset=slave bundle=gmem0 depth=8192
#pragma HLS INTERFACE ap_ctrl_none port=return
    hls_thread_local hls::task t(mem_w_stream_task, s_cmd, s_in, m_mem);
}
