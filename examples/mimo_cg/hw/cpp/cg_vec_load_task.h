// cg_vec_load_task.h -- CgVecUnit's loader: lands B and S_1 .. S_nit in lane-group blocks and plays
// cg_ctrl's part, issuing the vector unit's commands (INIT, then ITER .. LAST).
//
// Each burst is one K x N matrix in memory format (16-bit, the register's integer bits), deserialized
// with the generated array utils (read_framed_stream_lane) and converted to the register type by an
// ap_fixed assignment, which is exact because memory values come from that register format.  Elements
// arrive row-major, so lane groups complete in order: a group is built in registers and written once.
// Requires nit <= K and N % L == 0.  Python twin: CgVecLoad.run_iter (examples/mimo_cg/hw/vec.py).
#ifndef MIMO_CG_VEC_LOAD_TASK_H
#define MIMO_CG_VEC_LOAD_TASK_H
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
static void cg_vec_load_task(
    hls::stream<streamutils::framed_word<MEM_DW> >& s_in,
    hls::stream<streamutils::framed_word<MEM_DW> >& desc_out,
    hls::stream<streamutils::framed_word<MEM_DW> >& cmd_out,
    hls::stream_of_blocks<typename cg_lanes::group<cg::b_t, L>::type[K * N / L], SD>& b_blk,
    hls::stream_of_blocks<typename cg_lanes::group<cg::s_t, L>::type[K * N / L], SD>& s_blk) {
    typedef typename cg_lanes::group<cg::b_t, L>::type b_grp;
    typedef typename cg_lanes::group<cg::s_t, L>::type s_grp;
    CgDesc d;
    streamutils::tlast_status tl;
    d.read_framed_stream<MEM_DW>(s_in, tl);
    d.write_framed_stream<MEM_DW>(desc_out);
    const int nit = (int)d.nit;
    {
        hls::write_lock<b_grp[K * N / L]> b(b_blk);
        cg_load_matrix<MEM_DW, K, N, L, cg::b_t, cg::b_mem>(s_in, b, tl);
    }
    cg_send<MEM_DW>(cmd_out, IterOp::INIT, 0);
ITERS:
    for (int n = 1; n <= K; ++n) {
        if (n <= nit) {
            {
                hls::write_lock<s_grp[K * N / L]> s(s_blk);
                cg_load_matrix<MEM_DW, K, N, L, cg::s_t, cg::s_mem>(s_in, s, tl);
            }
            cg_send<MEM_DW>(cmd_out, n == nit ? IterOp::LAST : IterOp::ITER, n);
        }
    }
}

#endif  // MIMO_CG_VEC_LOAD_TASK_H
