#ifndef WAVEFLOW_BUILD_IL_LOAD_TASK_H
#define WAVEFLOW_BUILD_IL_LOAD_TASK_H
// il_load_task.h — stage 3 of the canonical six-stage interleaver: the stream->SOB bridge.  Read the
// per-job token, forward it, then write-lock-fill two resident blocks — p_blk from pwords, x_blk from
// xwords (each in its own write_lock scope so the depth-2 ping-pong overlaps).  No m_axi (DTLP clean).
// Copied verbatim by MemStreamStep, instantiated <MEM_DW, NW>.
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
#include "il_cmd.h"

template <int MEM_DW, int NW>
static void il_load_task(hls::stream<ap_uint<MEM_DW> >& cmd_in,
                         hls::stream<ap_uint<MEM_DW> >& pwords,
                         hls::stream<ap_uint<MEM_DW> >& xwords,
                         hls::stream<ap_uint<MEM_DW> >& cmd_out,
                         hls::stream_of_blocks<ap_uint<MEM_DW>[NW] >& p_blk,
                         hls::stream_of_blocks<ap_uint<MEM_DW>[NW] >& x_blk) {
    InterleaverCmd c;
    c.read_stream<MEM_DW>(cmd_in);
    c.write_stream<MEM_DW>(cmd_out);        // forward the token
    {
        hls::write_lock<ap_uint<MEM_DW>[NW] > pb(p_blk);
    LP: for (int w = 0; w < NW; ++w) {
#pragma HLS PIPELINE II=1
            pb[w] = pwords.read();
        }
    }
    {
        hls::write_lock<ap_uint<MEM_DW>[NW] > xb(x_blk);
    LX: for (int w = 0; w < NW; ++w) {
#pragma HLS PIPELINE II=1
            xb[w] = xwords.read();
        }
    }
}

#endif  // WAVEFLOW_BUILD_IL_LOAD_TASK_H
