// cg_vector_load_task.h -- the standalone CG vector unit's loader: one job per firing.
//
// Reads the job (CgVectorCmd) from the receiver, then the payloads it forwards, which are the job's
// accepted requests in order: B (k x n) into the core's B block, then each of the nit matrices S
// into its S block.  Python twin: waveflow.linalg.cg_vector.CgVectorLoad.
#ifndef WF_CG_VECTOR_LOAD_TASK_H
#define WF_CG_VECTOR_LOAD_TASK_H
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "wf_cg_vector_cmd.h"
#include "wf_lanes.h"
#include "wf_matrix_io.h"
#include "wf_linalg_traits.h"
#include "cg_vector_task.h"

template <int WBW, int KMAX, int NMAX, int NITMAX, int L, int SD, int FID, int IOID>
static void cg_vector_load_task(
    hls::stream<ap_uint<64> >& cmd_in, hls::stream<streamutils::framed_word<WBW> >& s_in,
    hls::stream_of_blocks<typename wf_lanes::group<typename wf_cg_traits<FID>::b_t, L>::type
                              [wf_cg::blk<KMAX, NMAX, L>::n],
                          SD>& b_blk,
    hls::stream_of_blocks<typename wf_lanes::group<typename wf_cg_traits<FID>::s_t, L>::type
                              [wf_cg::blk<KMAX, NMAX, L>::n],
                          SD>& s_blk) {
    typedef wf_cg_io_traits<IOID> IO;
    typedef typename wf_cg_traits<FID>::b_t b_t;
    typedef typename wf_cg_traits<FID>::s_t s_t;
    const int BG = wf_cg::blk<KMAX, NMAX, L>::n;
    const CgVectorCmd c = CgVectorCmd::unpack_from_uint(cmd_in.read());
    const int nit = c.nit, kn = c.k * c.n;
    streamutils::tlast_status tl;
    {
        hls::write_lock<typename wf_lanes::group<b_t, L>::type[BG]> b(b_blk);
        wf_load_matrix<WBW, L, BG, b_t, typename IO::b_mem>(s_in, b, kn, tl);
    }
ITERS:
    for (int it = 0; it < nit; ++it) {
#pragma HLS LOOP_TRIPCOUNT max = NITMAX
        hls::write_lock<typename wf_lanes::group<s_t, L>::type[BG]> s(s_blk);
        wf_load_matrix<WBW, L, BG, s_t, typename IO::s_mem>(s_in, s, kn, tl);
    }
}

#endif  // WF_CG_VECTOR_LOAD_TASK_H
