#ifndef WAVEFLOW_BUILD_FILL_TASK_H
#define WAVEFLOW_BUILD_FILL_TASK_H
// fill_task.h — the FIXED Fill body (the SOBIF producer), the generated analogue of the validated
// sandbox load_task (interleaver_sob_task.cpp).  A pure-AXIS, single-firing hls::task body (the
// runtime re-fires it per block; NO internal loop over jobs): write-lock a fresh block, fill it
// whole from the x_in stream, then release it (the '}' scope frees it immediately so the consumer can
// read-lock it while this task re-fires to fill the next block — the depth-2 ping-pong overlap).
// Element-granular (one ap_uint<EW> per stream transfer), width/size templated.  Copied verbatim by
// waveflow.build.streamutils.MemStreamStep and instantiated at concrete <EW, N> by the generated top.
// Verify via XSI — ap_ctrl_none Vitis cosim is unreliable.
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>

// x_in(AXIS) -> write_lock-fill a whole ap_uint<EW>[N] block. One element/cycle.
template <int EW, int N>
static void fill_task(hls::stream<ap_uint<EW> >& x_in,
                      hls::stream_of_blocks<ap_uint<EW>[N] >& x_blk) {
    hls::write_lock<ap_uint<EW>[N] > b(x_blk);
FILL: for (int i = 0; i < N; ++i) {
#pragma HLS PIPELINE II=1
        b[i] = x_in.read();
    }
}

#endif  // WAVEFLOW_BUILD_FILL_TASK_H
