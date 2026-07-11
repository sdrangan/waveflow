#ifndef WAVEFLOW_BUILD_GATHER_TASK_H
#define WAVEFLOW_BUILD_GATHER_TASK_H
// gather_task.h — the FIXED Gather body (the SOBIF consumer), the generated analogue of the validated
// sandbox gather_task (interleaver_sob_task.cpp).  A pure-AXIS, single-firing hls::task body (the
// runtime re-fires it per block): read-lock the filled block, then random-access it with the indices
// streamed on p_in and emit each gathered element on y_out.  The read_lock holds one ping-pong
// buffer while the producer fills the other — the overlap.  Element-granular: it indexes the block
// directly (b[p]); the word-granular LW-unroll gather (via elem_read<W>) is Phase 4.  Templated on
// <EW, N>; copied verbatim by MemStreamStep and instantiated at a concrete width by the generated top.
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>

// p_in(AXIS indices) x block -> y_out(AXIS): y[i] = b[p_in.read()]. One element/cycle (element-granular).
template <int EW, int N>
static void gather_task(hls::stream<ap_uint<EW> >& p_in,
                        hls::stream_of_blocks<ap_uint<EW>[N] >& x_blk,
                        hls::stream<ap_uint<EW> >& y_out) {
    hls::read_lock<ap_uint<EW>[N] > b(x_blk);
GATHER: for (int i = 0; i < N; ++i) {
#pragma HLS PIPELINE II=1
        y_out.write(b[p_in.read()]);
    }
}

#endif  // WAVEFLOW_BUILD_GATHER_TASK_H
