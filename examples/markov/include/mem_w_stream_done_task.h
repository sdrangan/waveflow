#ifndef WAVEFLOW_BUILD_MEM_W_STREAM_DONE_TASK_H
#define WAVEFLOW_BUILD_MEM_W_STREAM_DONE_TASK_H
// mem_w_stream_done_task.h — the FIXED s2a body WITH a completion echo, the composition variant of
// mem_w_stream_task.h (plans/mem_stream_impl.md Phase 2).  Identical pure-write burst, plus one
// MemComplete struct emitted on s_done after the burst — words written, plus the command's xfer_msg
// cookie echoed back unmodified — so a downstream/host can correlate per-job completion on the
// MemCopy composite.  Single-firing (the hls::task runtime re-fires per command); the sole m_axi
// WRITE owner still touches ONLY streams besides m_mem.  Copied verbatim by MemStreamStep and
// instantiated at a concrete width by the generated composite top.  Verify via XSI.
#include "hls_stream.h"
#include <ap_int.h>
#include "m_w_cmd.h"
#include "mem_complete.h"

// stream->AXI with done: pure-write len words (addr element coordinate — no byte<->word
// conversion, m_mem is a word pointer), then echo a MemComplete (words written + xfer_msg). Word rate.
template <int MEM_DW>
static void mem_w_stream_done_task(hls::stream<ap_uint<MEM_DW> >& s_cmd,
                                   hls::stream<ap_uint<MEM_DW> >& s_in,
                                   ap_uint<MEM_DW>* m_mem,
                                   hls::stream<ap_uint<MEM_DW> >& s_done) {
    MWCmd c;
    c.read_stream<MEM_DW>(s_cmd);
    const int w0 = (int)c.addr;
    const int nw = (int)c.len;
S2A: for (int w = 0; w < nw; ++w) {
#pragma HLS PIPELINE II=1
        m_mem[w0 + w] = s_in.read();
    }
    MemComplete comp;
    comp.len = nw;
    comp.xfer_len = c.xfer_len;
COPY_MSG: for (int i = 0; i < 8; ++i) {
#pragma HLS UNROLL
        comp.xfer_msg.data[i] = c.xfer_msg.data[i];
    }
    comp.write_stream<MEM_DW>(s_done);
}

#endif  // WAVEFLOW_BUILD_MEM_W_STREAM_DONE_TASK_H
