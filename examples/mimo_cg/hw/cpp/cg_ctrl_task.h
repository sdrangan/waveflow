// cg_ctrl_task.h -- CG control: per job, INIT and then nit iteration commands (ITER .. LAST) into
// each block's command queue, and the job's descriptor on to the store.  The queues are FIFOs whose
// depth is the composite's cmd_depth, so the control can run ahead of the blocks by that much.
// Requires nit <= K.  Python twin: CgCtrl.run_iter (examples/mimo_cg/hw/detector.py).
#ifndef MIMO_CG_CTRL_TASK_H
#define MIMO_CG_CTRL_TASK_H
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "cg_desc.h"
#include "cg_iter_cmd.h"

template <int MEM_DW, int K>
static void cg_ctrl_task(hls::stream<streamutils::framed_word<MEM_DW> >& desc_in,
                         hls::stream<streamutils::framed_word<MEM_DW> >& vec_cmd,
                         hls::stream<streamutils::framed_word<MEM_DW> >& mm_cmd,
                         hls::stream<streamutils::framed_word<MEM_DW> >& desc_out) {
    CgDesc d;
    streamutils::tlast_status tl;
    d.read_framed_stream<MEM_DW>(desc_in, tl);
    d.write_framed_stream<MEM_DW>(desc_out);
    const int nit = (int)d.nit;
CMDS:
    for (int n = 0; n <= K; ++n) {
        if (n <= nit) {
            CgIterCmd c;
            c.op = n == 0 ? IterOp::INIT : (n == nit ? IterOp::LAST : IterOp::ITER);
            c.it = n;
            c.write_framed_stream<MEM_DW>(vec_cmd);
            c.write_framed_stream<MEM_DW>(mm_cmd);
        }
    }
}

#endif  // MIMO_CG_CTRL_TASK_H
