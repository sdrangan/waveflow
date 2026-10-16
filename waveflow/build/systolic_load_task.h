// systolic_load_task.h -- the standalone systolic unit's loader: one job per firing.
//
// Reads the job (SystolicCmd) from the receiver, then the payload it forwards: A, landed in the
// core's A block as X = A (m x k), or for A^H transposed from k x m into X = A^T, a value per
// cycle at best (wf_load_matrix_transposed); then B (k x n) into the B block.
// Python twin: waveflow.linalg.systolic.SystolicLoad.
#ifndef WF_SYSTOLIC_LOAD_TASK_H
#define WF_SYSTOLIC_LOAD_TASK_H
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "wf_lanes.h"
#include "wf_matrix_io.h"
#include "wf_linalg_traits.h"
#include "wf_systolic_cmd.h"
#include "systolic_core_task.h"

template <int WBW, int MMAX, int KMAX, int NMAX, int L, int SD, int FID, int IOID>
static void systolic_load_task(
    hls::stream<ap_uint<64> >& cmd_in, hls::stream<streamutils::framed_word<WBW> >& s_in,
    hls::stream_of_blocks<typename wf_lanes::group<typename wf_systolic_traits<FID>::a_t, L>::type
                              [wf_systolic::blk<MMAX, KMAX, L>::n],
                          SD>& a_blk,
    hls::stream_of_blocks<typename wf_lanes::group<typename wf_systolic_traits<FID>::b_t, L>::type
                              [wf_systolic::blk<KMAX, NMAX, L>::n],
                          SD>& b_blk) {
    typedef wf_systolic_io_traits<IOID> IO;
    typedef typename wf_systolic_traits<FID>::a_t a_t;
    typedef typename wf_systolic_traits<FID>::b_t b_t;
    const int AG = wf_systolic::blk<MMAX, KMAX, L>::n;
    const int BG = wf_systolic::blk<KMAX, NMAX, L>::n;
    SystolicCmd c;
    c.template read_stream<64>(cmd_in);
    const int m = c.m, k = c.k, n = c.n;
    streamutils::tlast_status tl;
    {
        hls::write_lock<typename wf_lanes::group<a_t, L>::type[AG]> x(a_blk);
        if (c.op == wf_systolic::MUL_AH) {
            wf_load_matrix_transposed<WBW, L, AG, a_t, typename IO::a_mem>(s_in, x, k, m, tl);
        } else {
            wf_load_matrix<WBW, L, AG, a_t, typename IO::a_mem>(s_in, x, m * k, tl);
        }
    }
    {
        hls::write_lock<typename wf_lanes::group<b_t, L>::type[BG]> b(b_blk);
        wf_load_matrix<WBW, L, BG, b_t, typename IO::b_mem>(s_in, b, k * n, tl);
    }
}

#endif  // WF_SYSTOLIC_LOAD_TASK_H
