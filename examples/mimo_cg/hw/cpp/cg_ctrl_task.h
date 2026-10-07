// cg_ctrl_task.h -- CG control: per job, one command into each core's command queue and the job's
// descriptor on to the store.  The systolic core gets nb = nit matrices (A once, then one P in and
// one S out per iteration), the vector core nit iterations.  The queues are FIFOs whose depth is the
// composite's cmd_depth, so the control can run ahead of the cores by that many jobs.  Requires
// 1 <= nit <= K (cg_cmd_rx clamps it).  Python twin: CgCtrl.run_iter (examples/mimo_cg/hw/detector.py).
#ifndef MIMO_CG_CTRL_TASK_H
#define MIMO_CG_CTRL_TASK_H
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "cg_desc.h"
#include "wf_systolic_cmd.h"
#include "wf_cg_vector_cmd.h"
#include "systolic_core_task.h"  // wf_systolic::MUL

template <int MEM_DW, int K, int N>
static void cg_ctrl_task(hls::stream<streamutils::framed_word<MEM_DW> >& desc_in,
                         hls::stream<ap_uint<64> >& vec_cmd,
                         hls::stream<ap_uint<64> >& mm_cmd,
                         hls::stream<streamutils::framed_word<MEM_DW> >& desc_out) {
    CgDesc d;
    streamutils::tlast_status tl;
    d.read_framed_stream<MEM_DW>(desc_in, tl);
    d.write_framed_stream<MEM_DW>(desc_out);
    SystolicCmd m;
    m.op = wf_systolic::MUL;
    m.nb = d.nit;
    m.m = K;
    m.k = K;
    m.n = N;
    m.template write_stream<64>(mm_cmd);
    CgVectorCmd v;
    v.nit = d.nit;
    v.k = K;
    v.n = N;
    v.template write_stream<64>(vec_cmd);
}

#endif  // MIMO_CG_CTRL_TASK_H
