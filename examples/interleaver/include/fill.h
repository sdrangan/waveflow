#ifndef WAVEFLOW_EXAMPLES_FILL_H
#define WAVEFLOW_EXAMPLES_FILL_H
// fill.h — gather_toy stage 1: the stream->SOB bridge.  One task firing consumes BLOCK_N words
// from s_in and fills one resident block of m_out under a write_lock.  The lock is RAII and
// scoped: commit happens when it leaves scope, which is what frees the depth-2 ping-pong buffer
// for the consumer.  Mirrors the XSI-verified il_load_task.h.  No m_axi (free-running clean).
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>

template <int MEM_DW, int BLOCK_N>
static void fill(hls::stream<ap_uint<MEM_DW> >& s_in,
                 hls::stream_of_blocks<ap_uint<MEM_DW>[BLOCK_N] >& m_out) {
    hls::write_lock<ap_uint<MEM_DW>[BLOCK_N] > b(m_out);
LF: for (int i = 0; i < BLOCK_N; ++i) {
#pragma HLS PIPELINE II=1
        b[i] = s_in.read();
    }
}

#endif  // WAVEFLOW_EXAMPLES_FILL_H
