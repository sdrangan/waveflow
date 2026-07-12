#ifndef WAVEFLOW_BUILD_IL_MEM_W_TASK_H
#define WAVEFLOW_BUILD_IL_MEM_W_TASK_H
// il_mem_w_task.h — stage 6 of the canonical six-stage interleaver: the sole m_axi WRITE owner (gmem1).
// Read the per-job token, drain NW ywords and pure-write them to Y(y_off, NW), then emit the token on
// s_done — AFTER the write burst (the completion record, not merely issuing beats).  The token is not
// forwarded early here (it is the boundary completion); the write loop paces it to commit time.  A
// premature done would surface as a stale Y in the XSI bit-exact check.  Copied by MemStreamStep,
// instantiated <MEM_DW, NW>.  Word-granular; word_index coordinates.
#include "hls_stream.h"
#include <ap_int.h>
#include "il_cmd.h"

template <int MEM_DW, int NW>
static void il_mem_w_task(hls::stream<ap_uint<MEM_DW> >& cmd_in,
                          hls::stream<ap_uint<MEM_DW> >& ywords,
                          ap_uint<MEM_DW>* m_mem,
                          hls::stream<ap_uint<MEM_DW> >& s_done) {
    InterleaverCmd c;
    c.read_stream<MEM_DW>(cmd_in);
    const int yw = (int)c.y_off;
WY: for (int w = 0; w < NW; ++w) {
#pragma HLS PIPELINE II=1
        m_mem[yw + w] = ywords.read();
    }
    c.write_stream<MEM_DW>(s_done);         // completion: emit the token AFTER the write burst
}

#endif  // WAVEFLOW_BUILD_IL_MEM_W_TASK_H
