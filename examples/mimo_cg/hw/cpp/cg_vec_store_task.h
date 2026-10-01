// cg_vec_store_task.h -- CgVecUnit's storer: writes P_0 .. P_{nit-1} and then X, one MemWStream write
// per block.  The P writes forward nothing; the X write forwards the job's CgDesc, so the writer echoes
// exactly one completion per job on s_done.
//   [MemWCmd{out + n*NW, NW, fwd=0} | P_n]  for n < nit,  then  [MemWCmd{out + nit*NW, NW, fwd=1} | CgDesc | X]
// Register values are widened to the 16-bit memory format (exact) and serialized with the generated
// array utils (write_framed_stream_lane).  Requires nit <= K.
// Python twin: CgVecStore.run_iter (examples/mimo_cg/hw/vec.py).
#ifndef MIMO_CG_VEC_STORE_TASK_H
#define MIMO_CG_VEC_STORE_TASK_H
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "cg_desc.h"
#include "mem_w_cmd.h"
#include "cg_types.h"
#include "cg_lanes.h"
#include "cg_io.h"

template <int MEM_DW, int K, int N, int L, int SD>
static void cg_vec_store_task(
    hls::stream<streamutils::framed_word<MEM_DW> >& desc_in,
    hls::stream_of_blocks<typename cg_lanes::group<cg::p_t, L>::type[K * N / L], SD>& p_blk,
    hls::stream_of_blocks<typename cg_lanes::group<cg::x_t, L>::type[K * N / L], SD>& x_blk,
    hls::stream<streamutils::framed_word<MEM_DW> >& cmd_out) {
    typedef typename cg_lanes::group<cg::p_t, L>::type p_grp;
    typedef typename cg_lanes::group<cg::x_t, L>::type x_grp;
    const int LW = cg::p_mem::lane_capacity<MEM_DW>();
    const int NW = (K * N + LW - 1) / LW;
    CgDesc d;
    streamutils::tlast_status tl;
    d.read_framed_stream<MEM_DW>(desc_in, tl);
    const int nit = (int)d.nit;
    const int out = (int)d.x_off;
PS:
    for (int n = 0; n < K; ++n) {
        if (n < nit) {
            hls::read_lock<p_grp[K * N / L]> p(p_blk);
            MemWCmd w;
            w.addr = out + n * NW;
            w.len = NW;
            w.fwd_bursts = 0;
            w.write_framed_stream<MEM_DW>(cmd_out);
            cg_store_matrix<MEM_DW, K, N, L, cg::p_t, cg::p_mem>(p, cmd_out);
        }
    }
    {
        hls::read_lock<x_grp[K * N / L]> x(x_blk);
        MemWCmd w;
        w.addr = out + nit * NW;
        w.len = NW;
        w.fwd_bursts = 1;
        w.write_framed_stream<MEM_DW>(cmd_out);
        d.write_framed_stream<MEM_DW>(cmd_out);  // echoed on s_done after the store
        cg_store_matrix<MEM_DW, K, N, L, cg::x_t, cg::x_mem>(x, cmd_out);
    }
}

#endif  // MIMO_CG_VEC_STORE_TASK_H
