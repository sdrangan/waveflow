// cg_load_task.h -- the detector's loader: lands A (K row elements, for the matmul) and B (lane
// groups, for the vector unit) in blocks and passes the job's descriptor to the control.
// Python twin: CgLoad.run_iter (examples/mimo_cg/hw/detector.py).
#ifndef MIMO_CG_LOAD_TASK_H
#define MIMO_CG_LOAD_TASK_H
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "cg_desc.h"
#include "cg_types.h"
#include "cg_lanes.h"
#include "cg_io.h"

template <int MEM_DW, int K, int N, int L, int SD>
static void cg_load_task(
    hls::stream<streamutils::framed_word<MEM_DW> >& s_in,
    hls::stream<streamutils::framed_word<MEM_DW> >& desc_out,
    hls::stream_of_blocks<typename cg_lanes::group<cg::a_t, K>::type[K], SD>& a_blk,
    hls::stream_of_blocks<typename cg_lanes::group<cg::b_t, L>::type[K * N / L], SD>& b_blk) {
    typedef typename cg_lanes::group<cg::a_t, K>::type a_row;
    typedef typename cg_lanes::group<cg::b_t, L>::type b_grp;
    CgDesc d;
    streamutils::tlast_status tl;
    d.read_framed_stream<MEM_DW>(s_in, tl);
    d.write_framed_stream<MEM_DW>(desc_out);
    {
        hls::write_lock<a_row[K]> a(a_blk);
        cg_load_matrix<MEM_DW, K, K, K, cg::a_t, cg::a_mem>(s_in, a, tl);
    }
    {
        hls::write_lock<b_grp[K * N / L]> b(b_blk);
        cg_load_matrix<MEM_DW, K, N, L, cg::b_t, cg::b_mem>(s_in, b, tl);
    }
}

#endif  // MIMO_CG_LOAD_TASK_H
