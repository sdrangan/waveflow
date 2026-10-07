// cg_store_task.h -- the detector's storer: writes X, then a zero-length write that forwards the
// job's CgDesc, so the writer echoes exactly one completion per job on s_done.
//   [MemWCmd{x_off, NW, fwd=0} | X]  [MemWCmd{x_off, 0, fwd=1} | CgDesc]
// Two writes per job match the two reads (A, B): HLS couples the reader's and writer's firing counts
// through the m_axi pointer FIFOs, so a job with fewer writes than reads deadlocks after a few jobs
// (plan §15, step 4.7).  X is packed into the example's memory format (cg_types.h) by Waveflow's
// wf_store_matrix (wf_matrix_io.h).  Python twin: CgStore.run_iter (examples/mimo_cg/hw/detector.py).
#ifndef MIMO_CG_STORE_TASK_H
#define MIMO_CG_STORE_TASK_H
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "cg_desc.h"
#include "mem_w_cmd.h"
#include "cg_types.h"
#include "wf_lanes.h"
#include "wf_matrix_io.h"

template <int MEM_DW, int K, int N, int L, int SD>
static void cg_store_task(
    hls::stream<streamutils::framed_word<MEM_DW> >& desc_in,
    hls::stream_of_blocks<typename wf_lanes::group<cg::x_t, L>::type[K * N / L], SD>& x_blk,
    hls::stream<streamutils::framed_word<MEM_DW> >& cmd_out) {
    typedef typename wf_lanes::group<cg::x_t, L>::type x_grp;
    const int LW = cg::x_mem::lane_capacity<MEM_DW>();
    CgDesc d;
    streamutils::tlast_status tl;
    d.read_framed_stream<MEM_DW>(desc_in, tl);
    hls::read_lock<x_grp[K * N / L]> x(x_blk);
    MemWCmd w;
    w.addr = d.x_off;
    w.len = (K * N + LW - 1) / LW;
    w.fwd_bursts = 0;
    w.write_framed_stream<MEM_DW>(cmd_out);
    wf_store_matrix<MEM_DW, L, K * N / L, cg::x_t, cg::x_mem>(x, cmd_out, K * N);
    MemWCmd e;  // the zero-length write that carries the echo (see the header)
    e.addr = d.x_off;
    e.len = 0;
    e.fwd_bursts = 1;
    e.write_framed_stream<MEM_DW>(cmd_out);
    d.write_framed_stream<MEM_DW>(cmd_out);  // echoed on s_done after X is stored
}

#endif  // MIMO_CG_STORE_TASK_H
