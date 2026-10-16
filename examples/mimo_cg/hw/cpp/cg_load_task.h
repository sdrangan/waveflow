// cg_load_task.h -- the detector's loader: lands A (for the systolic core) and B (for the vector
// core) in blocks of row-major L-lane groups and passes the job's descriptor to the control.
// The matrices arrive in the example's memory format (cg_types.h) and are unpacked by Waveflow's
// wf_load_matrix (wf_matrix_io.h).  Python twin: CgLoad.run_iter (examples/mimo_cg/hw/detector.py).
#ifndef MIMO_CG_LOAD_TASK_H
#define MIMO_CG_LOAD_TASK_H
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "cg_desc.h"
#include "cg_types.h"
#include "wf_lanes.h"
#include "wf_matrix_io.h"

template <int MEM_DW, int K, int N, int L, int SD>
static void cg_load_task(
    hls::stream<streamutils::framed_word<MEM_DW> >& s_in,
    hls::stream<streamutils::framed_word<MEM_DW> >& desc_out,
    hls::stream_of_blocks<typename wf_lanes::group<cg::a_t, L>::type[(K * K + L - 1) / L], SD>& a_blk,
    hls::stream_of_blocks<typename wf_lanes::group<cg::b_t, L>::type[K * N / L], SD>& b_blk) {
    typedef typename wf_lanes::group<cg::a_t, L>::type a_grp;
    typedef typename wf_lanes::group<cg::b_t, L>::type b_grp;
    const int AG = (K * K + L - 1) / L;
    CgDesc d;
    streamutils::tlast_status tl;
    d.read_framed_stream<MEM_DW>(s_in, tl);
    d.write_framed_stream<MEM_DW>(desc_out);
    {
        hls::write_lock<a_grp[AG]> a(a_blk);
        wf_load_matrix<MEM_DW, L, AG, cg::a_t, cg::a_mem>(s_in, a, K * K, tl);
    }
    {
        hls::write_lock<b_grp[K * N / L]> b(b_blk);
        wf_load_matrix<MEM_DW, L, K * N / L, cg::b_t, cg::b_mem>(s_in, b, K * N, tl);
    }
}

#endif  // MIMO_CG_LOAD_TASK_H
