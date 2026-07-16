#include "mem_seq_task.h"

namespace mem_seq_impl {
// CopyCmd -> the write command, carrying the SAME job cookie as the read (msg is passed in, not
// re-stamped) so both halves of a copy correlate to one job.
// Golden: examples/interleaver/mem_copy.py::Sequencer.make_mw_cmd.
MWCmd make_mw_cmd(CopyCmd cmd, UInt32Array msg) {
#pragma HLS INLINE
    MWCmd w;
    w.addr     = cmd.dst_off;
    w.len      = cmd.n_words;
    w.xfer_len = 1;
    w.xfer_msg = msg;
    return w;
}
}
