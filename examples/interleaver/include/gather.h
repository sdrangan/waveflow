#ifndef WAVEFLOW_EXAMPLES_GATHER_H
#define WAVEFLOW_EXAMPLES_GATHER_H
// gather.h — gather_toy stage 2: the SOB->stream bridge.  One task firing read_locks one
// committed block of s_in and streams its BLOCK_N words out to m_out.  The RAII lock releases
// the block back to the producer when it leaves scope.  Mirrors the XSI-verified il_store_task.h.
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>

template <int MEM_DW, int BLOCK_N>
static void gather(hls::stream_of_blocks<ap_uint<MEM_DW> [BLOCK_N] >& s_in,
                   hls::stream<ap_uint<MEM_DW> >& m_out) {
    hls::read_lock<ap_uint<MEM_DW>[BLOCK_N] > b(s_in);
LG: for (int i = 0; i < BLOCK_N; ++i) {
#pragma HLS PIPELINE II=1
        m_out.write(b[i]);
    }
}

#endif  // WAVEFLOW_EXAMPLES_GATHER_H
