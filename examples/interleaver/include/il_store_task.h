#ifndef WAVEFLOW_BUILD_IL_STORE_TASK_H
#define WAVEFLOW_BUILD_IL_STORE_TASK_H
// il_store_task.h — stage 5 of the canonical six-stage interleaver: the SOB->stream bridge.  Read the
// per-job token, forward it, then read-lock y_blk and stream its NW words out to ywords.  No m_axi.
// Copied verbatim by MemStreamStep, instantiated <MEM_DW, NW>.
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
#include "il_cmd.h"

template <int MEM_DW, int NW>
static void il_store_task(hls::stream<ap_uint<MEM_DW> >& cmd_in,
                          hls::stream_of_blocks<ap_uint<MEM_DW>[NW] >& y_blk,
                          hls::stream<ap_uint<MEM_DW> >& cmd_out,
                          hls::stream<ap_uint<MEM_DW> >& ywords) {
    InterleaverCmd c;
    c.read_stream<MEM_DW>(cmd_in);
    c.write_stream<MEM_DW>(cmd_out);        // forward the token
    hls::read_lock<ap_uint<MEM_DW>[NW] > yb(y_blk);
ST: for (int w = 0; w < NW; ++w) {
#pragma HLS PIPELINE II=1
        ywords.write(yb[w]);
    }
}

#endif  // WAVEFLOW_BUILD_IL_STORE_TASK_H
