// cg_mm_store_task.h -- CgMmUnit's storer: writes S_1 .. S_nit, one MemWStream write per block; only
// the last forwards the job's CgDesc, so the writer echoes exactly one completion per job.
// Requires nit <= K.  Python twin: CgMmStore.run_iter (examples/mimo_cg/hw/mm.py).
#ifndef MIMO_CG_MM_STORE_TASK_H
#define MIMO_CG_MM_STORE_TASK_H
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
static void cg_mm_store_task(
    hls::stream<streamutils::framed_word<MEM_DW> >& desc_in,
    hls::stream_of_blocks<typename cg_lanes::group<cg::s_t, L>::type[K * N / L], SD>& s_blk,
    hls::stream<streamutils::framed_word<MEM_DW> >& cmd_out) {
    typedef typename cg_lanes::group<cg::s_t, L>::type s_grp;
    const int LW = cg::s_mem::lane_capacity<MEM_DW>();
    const int NW = (K * N + LW - 1) / LW;
    CgDesc d;
    streamutils::tlast_status tl;
    d.read_framed_stream<MEM_DW>(desc_in, tl);
    const int nit = (int)d.nit;
    const int out = (int)d.x_off;
SS:
    for (int n = 0; n < K; ++n) {
        if (n < nit) {
            hls::read_lock<s_grp[K * N / L]> s(s_blk);
            const bool last = n == nit - 1;
            MemWCmd w;
            w.addr = out + n * NW;
            w.len = NW;
            w.fwd_bursts = last ? 1 : 0;
            w.write_framed_stream<MEM_DW>(cmd_out);
            if (last) d.write_framed_stream<MEM_DW>(cmd_out);  // echoed on s_done after the store
            cg_store_matrix<MEM_DW, K, N, L, cg::s_t, cg::s_mem>(s, cmd_out);
        }
    }
}

#endif  // MIMO_CG_MM_STORE_TASK_H
