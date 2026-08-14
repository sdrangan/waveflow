#ifndef BRAM_TOY_BRAM_WRITE_TASK_H
#define BRAM_TOY_BRAM_WRITE_TASK_H
// bram_write_task — one word per firing, into a memory that lives OUTSIDE the kernel.
//
// Hand-written, and it stays hand-written for the same reason `mem_r_stream_task` does: the body
// owns a resource the extractor has no vocabulary for.  There it is `m_axi`; here it is a `bram`
// array parameter and a static write pointer.  Its Python twin (`BramWrite.run_iter`) is the pysim
// golden, not the source of this file.
//
// This is the witness's `write_task` (plans/witness/t2p_bram/rx_kernel.cpp) plus one thing: a token
// on `go` once the buffer has been filled.  That token is what makes the reader's answers
// deterministic — see bram_read_task.h.
#include "hls_stream.h"
#include <ap_int.h>

/// @tparam W     payload width in bits
/// @tparam N     memory depth in words (the ARRAY SIZE: `mode=bram` on an unsized pointer silently
///               degrades to an ap_vld scalar port, so this is load-bearing, not decoration)
/// @tparam FILL  words written before the "buffer ready" token is emitted
template <int W, int N, int FILL>
void bram_write_task(ap_uint<W> buf_w[N], hls::stream<ap_uint<W> >& rx,
                     hls::stream<ap_uint<W> >& go) {
    // The write pointer persists across firings — an hls::task body is re-fired, not re-entered,
    // so a static is the state.  Exactly the witness's `static ap_uint<10> wr`.
    static ap_uint<32> wr = 0;

    ap_uint<W> x = rx.read();
    buf_w[wr] = x;

    if (wr == FILL - 1) {
        // One token, once: the buffer now holds FILL words, so a reader may look at any of them.
        go.write(1);
    }
    wr = (wr == N - 1) ? (ap_uint<32>)0 : (ap_uint<32>)(wr + 1);
}

#endif  // BRAM_TOY_BRAM_WRITE_TASK_H
