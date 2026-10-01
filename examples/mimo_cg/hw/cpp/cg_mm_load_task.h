// cg_mm_load_task.h -- CgMmUnit's loader: lands A (as K row elements) and P_0 .. P_{nit-1} (as lane
// groups) in blocks, forwards the descriptor, and issues the matmul's commands (INIT, ITER .. LAST).
// Requires nit <= K.  Python twin: CgMmLoad.run_iter (examples/mimo_cg/hw/mm.py).
#ifndef MIMO_CG_MM_LOAD_TASK_H
#define MIMO_CG_MM_LOAD_TASK_H
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "cg_desc.h"
#include "cg_iter_cmd.h"
#include "cg_types.h"
#include "cg_lanes.h"
#include "cg_io.h"

template <int MEM_DW, int K, int N, int L, int SD>
static void cg_mm_load_task(
    hls::stream<streamutils::framed_word<MEM_DW> >& s_in,
    hls::stream<streamutils::framed_word<MEM_DW> >& desc_out,
    hls::stream<streamutils::framed_word<MEM_DW> >& cmd_out,
    hls::stream_of_blocks<typename cg_lanes::group<cg::a_t, K>::type[K], SD>& a_blk,
    hls::stream_of_blocks<typename cg_lanes::group<cg::p_t, L>::type[K * N / L], SD>& p_blk) {
    typedef typename cg_lanes::group<cg::a_t, K>::type a_row;
    typedef typename cg_lanes::group<cg::p_t, L>::type p_grp;
    CgDesc d;
    streamutils::tlast_status tl;
    d.read_framed_stream<MEM_DW>(s_in, tl);
    d.write_framed_stream<MEM_DW>(desc_out);
    const int nit = (int)d.nit;
    {
        hls::write_lock<a_row[K]> a(a_blk);
        cg_load_matrix<MEM_DW, K, K, K, cg::a_t, cg::a_mem>(s_in, a, tl);  // a row is a K-lane group
    }
    cg_send<MEM_DW>(cmd_out, IterOp::INIT, 0);
ITERS:
    for (int n = 1; n <= K; ++n) {
        if (n <= nit) {
            {
                hls::write_lock<p_grp[K * N / L]> p(p_blk);
                cg_load_matrix<MEM_DW, K, N, L, cg::p_t, cg::p_mem>(s_in, p, tl);
            }
            cg_send<MEM_DW>(cmd_out, n == nit ? IterOp::LAST : IterOp::ITER, n);
        }
    }
}

#endif  // MIMO_CG_MM_LOAD_TASK_H
