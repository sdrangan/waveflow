#include "mem_seq_task.h"

namespace mem_seq_impl {
// CopyCmd -> the read command.  Element coordinates pass through verbatim (no byte<->word
// conversion) -- the addressing convention, plans/component.md.
// Golden: examples/interleaver/mem_copy.py::Sequencer.make_mr_cmd.
MRCmd make_mr_cmd(CopyCmd cmd, UInt32Array msg) {
#pragma HLS INLINE
    MRCmd r;
    r.addr     = cmd.src_off;
    r.len      = cmd.n_words;
    r.xfer_len = 1;
    r.xfer_msg = msg;
    return r;
}
}
