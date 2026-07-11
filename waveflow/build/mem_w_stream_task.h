#ifndef WAVEFLOW_BUILD_MEM_W_STREAM_TASK_H
#define WAVEFLOW_BUILD_MEM_W_STREAM_TASK_H
// mem_w_stream_task.h — the FIXED s2a body (the validated sandbox interleaver_task_sob3.cpp), the
// mirror of mem_r_stream_task.h: a width-templated, single-firing hls::task body (the runtime
// re-fires it per command; NO internal command loop); word-granular pure-write burst.  Copied into a
// kernel's include dir by waveflow.build.streamutils.MemStreamStep and instantiated at a concrete
// width by the generated top.
//
// The sole m_axi WRITE owner touches ONLY streams.  Verify via XSI — ap_ctrl_none cosim is unreliable.
#include "hls_stream.h"
#include <ap_int.h>
#include "m_w_cmd.h"

// stream->AXI: dequeue one MWCmd, pure-write n_words words. word_index is an element/word coordinate
// (m_mem is already a word pointer, so no byte<->word conversion — the offset=slave base + AXI HW
// turn m_mem[word_index] into AWADDR = base + word_index*(MEM_DW/8)). Word rate.
template <int MEM_DW>
static void mem_w_stream_task(hls::stream<ap_uint<MEM_DW> >& s_cmd,
                              hls::stream<ap_uint<MEM_DW> >& s_in,
                              ap_uint<MEM_DW>* m_mem) {
    MWCmd c;
    c.read_stream<MEM_DW>(s_cmd);
    const int w0 = (int)c.word_index;
    const int nw = (int)c.n_words;
S2A: for (int w = 0; w < nw; ++w) {
#pragma HLS PIPELINE II=1
        m_mem[w0 + w] = s_in.read();
    }
}

#endif  // WAVEFLOW_BUILD_MEM_W_STREAM_TASK_H
