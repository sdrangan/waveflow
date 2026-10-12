// cg_vector_store_task.h -- the standalone CG vector unit's store: one reply per firing.
//
// Reads the reply header the receiver prepared and writes it as the reply's header burst.  For an
// accepted request (status OK) it then writes, as one burst of memory elements (k x n), the core's
// next P (the reply to a START, or to a STEP with steps still to come: nfollow > 0) or X (the reply
// to the job's last STEP).  A rejected request's reply is the header alone.
// Python twin: waveflow.linalg.cg_vector.CgVectorStore.
#ifndef WF_CG_VECTOR_STORE_TASK_H
#define WF_CG_VECTOR_STORE_TASK_H
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "wf_lanes.h"
#include "wf_matrix_io.h"
#include "wf_linalg_msg.h"
#include "wf_linalg_traits.h"
#include "cg_vector_task.h"

template <int WBW, int KMAX, int NMAX, int L, int SD, int FID, int IOID>
static void cg_vector_store_task(
    hls::stream<streamutils::framed_word<WBW> >& cmd_in,
    hls::stream_of_blocks<typename wf_lanes::group<typename wf_cg_traits<FID>::p_t, L>::type
                              [wf_cg::blk<KMAX, NMAX, L>::n],
                          SD>& p_blk,
    hls::stream_of_blocks<typename wf_lanes::group<typename wf_cg_traits<FID>::x_t, L>::type
                              [wf_cg::blk<KMAX, NMAX, L>::n],
                          SD>& x_blk,
    hls::stream<streamutils::framed_word<WBW> >& s_out) {
    typedef wf_cg_io_traits<IOID> IO;
    typedef typename wf_cg_traits<FID>::p_t p_t;
    typedef typename wf_cg_traits<FID>::x_t x_t;
    const int BG = wf_cg::blk<KMAX, NMAX, L>::n;
    LinalgHeader r;
    streamutils::tlast_status tl;
    r.template read_framed_stream<WBW>(cmd_in, tl);
    r.template write_framed_stream<WBW>(s_out);
    if (r.status == wf_linalg::OK) {
        const int kn = r.k * r.n;
        if (r.op == wf_cg::START || r.nfollow != 0) {
            hls::read_lock<typename wf_lanes::group<p_t, L>::type[BG]> p(p_blk);
            wf_store_matrix<WBW, L, BG, p_t, typename IO::p_mem>(p, s_out, kn);
        } else {
            hls::read_lock<typename wf_lanes::group<x_t, L>::type[BG]> x(x_blk);
            wf_store_matrix<WBW, L, BG, x_t, typename IO::x_mem>(x, s_out, kn);
        }
    }
}

#endif  // WF_CG_VECTOR_STORE_TASK_H
