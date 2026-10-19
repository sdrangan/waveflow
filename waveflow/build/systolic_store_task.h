// systolic_store_task.h -- the standalone systolic unit's store: one reply per firing.
//
// Reads the reply header the receiver prepared and writes it as the reply's header burst; for a
// served request (status OK) it then writes C (m x n) from the core's C block as one burst of
// memory elements.  A rejected request's reply is the header alone.  OUT_T is s_out's word type
// (KernelTask.framed): framed_word inside a composite, axi4s_word at the unit's own top.
// Python twin: waveflow.linalg.systolic.SystolicStore.
#ifndef WF_SYSTOLIC_STORE_TASK_H
#define WF_SYSTOLIC_STORE_TASK_H
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "wf_lanes.h"
#include "wf_matrix_io.h"
#include "wf_linalg_msg.h"
#include "wf_linalg_traits.h"
#include "systolic_core_task.h"

template <int WBW, int MMAX, int KMAX, int NMAX, int L, int SD, int FID, int IOID, typename OUT_T>
static void systolic_store_task(
    hls::stream<streamutils::framed_word<WBW> >& cmd_in,
    hls::stream_of_blocks<typename wf_lanes::group<typename wf_systolic_traits<FID>::c_t, L>::type
                              [wf_systolic::blk<MMAX, NMAX, L>::n],
                          SD>& c_blk,
    hls::stream<OUT_T>& s_out) {
    typedef wf_systolic_io_traits<IOID> IO;
    typedef typename wf_systolic_traits<FID>::c_t c_t;
    const int CG = wf_systolic::blk<MMAX, NMAX, L>::n;
    LinalgHeader r;
    streamutils::tlast_status tl;
    r.template read_framed_stream<WBW>(cmd_in, tl);
    streamutils::write_msg<WBW>(r, s_out);
    if (r.status == wf_linalg::OK) {
        hls::read_lock<typename wf_lanes::group<c_t, L>::type[CG]> c(c_blk);
        wf_store_matrix<WBW, L, CG, c_t, typename IO::c_mem>(c, s_out, r.m * r.n);
    }
}

#endif  // WF_SYSTOLIC_STORE_TASK_H
