#ifndef EXAMPLES_RING_STUB_INC_TASK_H
#define EXAMPLES_RING_STUB_INC_TASK_H
// ring_inc_task.h -- the ring stub's unit K (HAND-WRITTEN), the HLS twin of
// examples/ring_stub/ring_stub.py::Inc.  ONE FIRING = ONE FRAME: each word plus 1 (modulo 2^64), up
// to and including the word with TLAST.  K knows nothing of jobs, passes or tags.
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "uint64_array_utils.h"

#ifndef RING_MAXN
#define RING_MAXN 64
#endif

template <int W, typename TI, typename TO>
static void ring_inc_task(hls::stream<TI>& s_in, hls::stream<TO>& m_out) {
    static_assert(W == 64, "ring_inc_task: one 64-bit element a word");
INC:
    for (int i = 0; i < RING_MAXN; ++i) {
#pragma HLS PIPELINE II=1
        uint64_array_utils::value_type v[1];
        streamutils::tlast_status ts = streamutils::tlast_status::no_tlast;
        uint64_array_utils::read_boundary_lane<W>(s_in, v, 1, ts);
        v[0] = v[0] + 1;
        const bool last = (ts != streamutils::tlast_status::no_tlast) || (i == RING_MAXN - 1);
        uint64_array_utils::write_boundary_lane<W>(v, m_out, last, 1);
        if (last) {
            break;
        }
    }
}

#endif  // EXAMPLES_RING_STUB_INC_TASK_H
