#ifndef WAVEFLOW_BUILD_MEM_W_STREAM_DONE_TASK_H
#define WAVEFLOW_BUILD_MEM_W_STREAM_DONE_TASK_H
// mem_w_stream_done_task.h — the FIXED s2a body WITH a completion token, the composition variant of
// mem_w_stream_task.h (plans/mem_stream_impl.md Phase 2).  Identical pure-write burst, plus one
// s_done word emitted after the burst so a downstream/host can observe per-job completion on the
// MemCopy composite.  Single-firing (the hls::task runtime re-fires per command); the sole m_axi
// WRITE owner still touches ONLY streams besides m_mem.  Copied verbatim by MemStreamStep and
// instantiated at a concrete width by the generated composite top.  Verify via XSI.
#include "hls_stream.h"
#include <ap_int.h>
#include "m_w_cmd.h"

// stream->AXI with done: pure-write n_words words (word_index element coordinate — no byte<->word
// conversion, m_mem is a word pointer), then emit one completion token (= words written). Word rate.
template <int MEM_DW>
static void mem_w_stream_done_task(hls::stream<ap_uint<MEM_DW> >& s_cmd,
                                   hls::stream<ap_uint<MEM_DW> >& s_in,
                                   ap_uint<MEM_DW>* m_mem,
                                   hls::stream<ap_uint<MEM_DW> >& s_done) {
    MWCmd c;
    c.read_stream<MEM_DW>(s_cmd);
    const int w0 = (int)c.word_index;
    const int nw = (int)c.n_words;
S2A: for (int w = 0; w < nw; ++w) {
#pragma HLS PIPELINE II=1
        m_mem[w0 + w] = s_in.read();
    }
    s_done.write((ap_uint<MEM_DW>)nw);
}

#endif  // WAVEFLOW_BUILD_MEM_W_STREAM_DONE_TASK_H
