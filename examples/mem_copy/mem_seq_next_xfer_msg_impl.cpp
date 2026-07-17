#include "mem_seq_task.h"

namespace mem_seq_impl {
// The Sequencer's only cross-firing state.  It lives here rather than in the generated body because
// a lowered body may not read mutable self.X -- the @synthesizable boundary is what makes run_iter
// extractable, and a hook's hand-written C++ is where a `static` is allowed to be.  The hls::task
// runtime re-invokes the body per command without resetting its frame, so this persists per job.
// Golden: examples/mem_copy/mem_copy.py::Sequencer.next_xfer_msg.
UInt32Array next_xfer_msg() {
#pragma HLS INLINE
    static ap_uint<32> job_idx = 0;
    UInt32Array msg;
INIT_MSG: for (int i = 0; i < 8; ++i) {
#pragma HLS UNROLL
        msg.data[i] = 0;
    }
    msg.data[0] = job_idx;
    ++job_idx;
    return msg;
}
}
