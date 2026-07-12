#ifndef WAVEFLOW_BUILD_IL_MEM_R_TASK_H
#define WAVEFLOW_BUILD_IL_MEM_R_TASK_H
// il_mem_r_task.h — stage 2 of the canonical six-stage interleaver: the sole m_axi READ owner (gmem0).
// Read the per-job token, forward it (before the work, sob3-style so the pipeline overlaps), then
// burst P(p_off, NW) -> pwords and X(x_off, NW) -> xwords from m_mem (two output streams — no Demux).
// Touches only streams + m_axi (DTLP clean).  Word-granular (ap_uint<MEM_DW>); word_index coordinates
// (m_mem is a word pointer, no byte<->word).  Copied verbatim by MemStreamStep, instantiated <MEM_DW, NW>.
#include "hls_stream.h"
#include <ap_int.h>
#include "il_cmd.h"

template <int MEM_DW, int NW>
static void il_mem_r_task(hls::stream<ap_uint<MEM_DW> >& cmd_in,
                          const ap_uint<MEM_DW>* m_mem,
                          hls::stream<ap_uint<MEM_DW> >& cmd_out,
                          hls::stream<ap_uint<MEM_DW> >& pwords,
                          hls::stream<ap_uint<MEM_DW> >& xwords) {
    InterleaverCmd c;
    c.read_stream<MEM_DW>(cmd_in);
    c.write_stream<MEM_DW>(cmd_out);        // forward the token
    const int pw = (int)c.p_off;
    const int xw = (int)c.x_off;
RDP: for (int w = 0; w < NW; ++w) {
#pragma HLS PIPELINE II=1
        pwords.write(m_mem[pw + w]);
    }
RDX: for (int w = 0; w < NW; ++w) {
#pragma HLS PIPELINE II=1
        xwords.write(m_mem[xw + w]);
    }
}

#endif  // WAVEFLOW_BUILD_IL_MEM_R_TASK_H
