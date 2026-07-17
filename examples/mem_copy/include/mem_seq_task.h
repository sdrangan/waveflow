#ifndef WAVEFLOW_GEN_MEM_SEQ_TASK_H
#define WAVEFLOW_GEN_MEM_SEQ_TASK_H
// mem_seq_task.h -- GENERATED from Sequencer.run_iter by waveflow (build/hwgen.py::task_files_to_str).  DO NOT EDIT: regenerate instead.
// A single firing = one hls::task invocation; the runtime re-fires it, so there is no
// command loop here and no INTERFACE pragma (the composite top owns the interface).
// The @synthesizable hook bodies are HAND-WRITTEN and are not lowered from the Python.
#include "hls_stream.h"
#include <ap_int.h>
#include "copy_cmd.h"
#include "u_int32_array.h"
#include "m_r_cmd.h"
#include "m_w_cmd.h"

namespace mem_seq_impl {
    UInt32Array next_xfer_msg();
    MRCmd make_mr_cmd(CopyCmd cmd, UInt32Array msg);
    MWCmd make_mw_cmd(CopyCmd cmd, UInt32Array msg);
}

template <int MEM_DWIDTH>
static void mem_seq_task(
    hls::stream<ap_uint<MEM_DWIDTH> >& s_cmd,
    hls::stream<ap_uint<MEM_DWIDTH> >& mr_cmd,
    hls::stream<ap_uint<MEM_DWIDTH> >& mw_cmd
) {
    CopyCmd cmd;
    cmd.read_stream<MEM_DWIDTH>(s_cmd);
    UInt32Array msg = mem_seq_impl::next_xfer_msg();
    MRCmd mr = mem_seq_impl::make_mr_cmd(cmd, msg);
    mr.write_stream<MEM_DWIDTH>(mr_cmd);
    MWCmd mw = mem_seq_impl::make_mw_cmd(cmd, msg);
    mw.write_stream<MEM_DWIDTH>(mw_cmd);
}

#endif  // WAVEFLOW_GEN_MEM_SEQ_TASK_H
