#include "mem_seq_task.h"

namespace mem_seq_impl {
// The correlation cookie: xfer_msg[0] = the command's tx_id (the host's transaction ID), echoed back
// unchanged on MemComplete so the host can match a completion to the exact request it issued.  The
// value comes from the command, so there is no cross-firing state here -- no `static` counter.
// Golden: examples/mem_copy/mem_copy.py::Sequencer.make_xfer_msg.
UInt32Array make_xfer_msg(CopyCmd cmd) {
#pragma HLS INLINE
    UInt32Array msg;
INIT_MSG: for (int i = 0; i < 8; ++i) {
#pragma HLS UNROLL
        msg.data[i] = 0;
    }
    msg.data[0] = cmd.tx_id;
    return msg;
}
}
