#ifndef WAVEFLOW_BUILD_MEM_R_STREAM_TASK_H
#define WAVEFLOW_BUILD_MEM_R_STREAM_TASK_H
// mem_r_stream_task.h — the FIXED a2s body (the validated sandbox interleaver_task_sob3.cpp), as a
// width-templated, single-firing hls::task body.  The hls::task runtime RE-FIRES this on each new
// command (there is NO internal command loop); word-granular (ap_uint<MEM_DW>).  Copied verbatim into a
// kernel's include dir by waveflow.build.streamutils.MemStreamStep and instantiated at a concrete
// width by the generated top (mem_r_stream_task<64>), consistent with the <word_bw>-templated
// array_utils / streamutils primitives.
//
// The sole m_axi READ owner touches ONLY streams (the DTLP / hls::task+m_axi de-risk: an m_axi owner
// never also locks a stream_of_blocks).  Verify via XSI — ap_ctrl_none Vitis cosim is unreliable.
#include "hls_stream.h"
#include <ap_int.h>
#include "memmgr.hpp"
#include "m_r_cmd.h"

// AXI->stream: dequeue one MRCmd, burst n_words words out. word_index is an element/word coordinate
// (m_mem is already a word pointer, so no byte<->word conversion — the offset=slave base + AXI HW
// turn m_mem[word_index] into ARADDR = base + word_index*(MEM_DW/8)). Word rate.
template <int MEM_DW>
static void mem_r_stream_task(hls::stream<ap_uint<MEM_DW> >& s_cmd,
                              const ap_uint<MEM_DW>* m_mem,
                              hls::stream<ap_uint<MEM_DW> >& m_out) {
    MRCmd c;
    c.read_stream<MEM_DW>(s_cmd);
    const int w0 = (int)c.word_index;
    const int nw = (int)c.n_words;
A2S: for (int w = 0; w < nw; ++w) {
#pragma HLS PIPELINE II=1
        m_out.write(m_mem[w0 + w]);
    }
}

#endif  // WAVEFLOW_BUILD_MEM_R_STREAM_TASK_H
