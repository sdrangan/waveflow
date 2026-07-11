#ifndef WAVEFLOW_BUILD_DEMUX_TASK_H
#define WAVEFLOW_BUILD_DEMUX_TASK_H
// demux_task.h — the FIXED Demux body for the full interleaver (Phase 4).  A pure-stream,
// single-firing hls::task body (the runtime re-fires it per job): the MemRStream bursts P then X
// (NW words each) on one m_out; this splits that 2*NW-word run into p_words (first NW) + x_words
// (next NW) by the compile-time split count NW — the same NW the Sequencer baked into the two MRCmds
// (single source of truth, no TLAST).  Word-granular (ap_uint<MEM_DW>).  Copied verbatim by
// MemStreamStep and instantiated <MEM_DW, NW> by the generated top.
#include "hls_stream.h"
#include <ap_int.h>

// mem_in (P words then X words) -> p_words (first NW), x_words (next NW). Word rate.
template <int MEM_DW, int NW>
static void demux_task(hls::stream<ap_uint<MEM_DW> >& mem_in,
                       hls::stream<ap_uint<MEM_DW> >& p_words,
                       hls::stream<ap_uint<MEM_DW> >& x_words) {
DP: for (int w = 0; w < NW; ++w) {
#pragma HLS PIPELINE II=1
        p_words.write(mem_in.read());
    }
DX: for (int w = 0; w < NW; ++w) {
#pragma HLS PIPELINE II=1
        x_words.write(mem_in.read());
    }
}

#endif  // WAVEFLOW_BUILD_DEMUX_TASK_H
