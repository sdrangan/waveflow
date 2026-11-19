#ifndef WAVEFLOW_BUILD_MEM_R_STREAM_DONE_TASK_H
#define WAVEFLOW_BUILD_MEM_R_STREAM_DONE_TASK_H
// mem_r_stream_done_task.h — the FIXED a2s body WITH a completion echo, the composition variant of
// mem_r_stream_task.h.  Identical burst-read, plus one MemComplete struct (the words read) emitted
// on s_done after the burst, one per command.  Single-firing (the hls::task runtime re-fires
// per command); the sole m_axi READ owner still touches ONLY streams besides m_mem.  Copied verbatim
// by MemStreamStep and instantiated at a concrete width by the generated composite top.  Verify via
// XSI.
#include "hls_stream.h"
#include <ap_int.h>
#include "m_r_cmd.h"
#include "mem_complete.h"

// AXI->stream with done: burst len words out (addr element coordinate — no byte<->word conversion,
// m_mem is a word pointer), then send a MemComplete (words read). Word rate.
template <int MEM_DW>
static void mem_r_stream_done_task(hls::stream<ap_uint<MEM_DW> >& s_cmd,
                                   const ap_uint<MEM_DW>* m_mem,
                                   hls::stream<ap_uint<MEM_DW> >& m_out,
                                   hls::stream<ap_uint<MEM_DW> >& s_done) {
    MRCmd c;
    c.read_stream<MEM_DW>(s_cmd);
    const int w0 = (int)c.addr;
    const int nw = (int)c.len;
A2S: for (int w = 0; w < nw; ++w) {
#pragma HLS PIPELINE II=1
        m_out.write(m_mem[w0 + w]);
    }
    MemComplete comp;
    comp.len = nw;
    comp.write_stream<MEM_DW>(s_done);
}

#endif  // WAVEFLOW_BUILD_MEM_R_STREAM_DONE_TASK_H
