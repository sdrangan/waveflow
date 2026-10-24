// systolic_engine_store_task.h -- the systolic engine's store: one command per firing.
//
// Reads the reply the receiver prepared; per output, its EngineResp (tx_id, index, m, n) as one burst
// on s_out, then C (m x n) from the core's C block as one burst of memory elements; then the
// command's EngineStatus on m_status.  A refused command has no outputs: the status alone.
// OUT_T / ST_T are s_out's and m_status's word types (KernelTask.framed).
// Python twin: waveflow.linalg.systolic_engine.EngineStore.
#ifndef WF_SYSTOLIC_ENGINE_STORE_TASK_H
#define WF_SYSTOLIC_ENGINE_STORE_TASK_H
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "wf_lanes.h"
#include "wf_matrix_io.h"
#include "wf_linalg_traits.h"
#include "wf_engine_reply.h"
#include "wf_engine_resp.h"
#include "wf_engine_status.h"

template <int WBW, int MMAX, int KMAX, int NMAX, int PMAX, int L, int R, int C, int SD, int FID,
          int IOID, typename OUT_T, typename ST_T>
static void systolic_engine_store_task(
    hls::stream<ap_uint<64> >& reply,
    hls::stream_of_blocks<typename wf_lanes::group<typename wf_systolic_traits<FID>::c_t, L>::type
                              [MMAX * NMAX / L],
                          SD>& c_blk,
    hls::stream<OUT_T>& s_out, hls::stream<ST_T>& m_status) {
    typedef wf_systolic_io_traits<IOID> IO;
    typedef typename wf_systolic_traits<FID>::c_t c_t;
    const int CGN = MMAX * NMAX / L;
    EngineReply r;
    r.template read_stream<64>(reply);
    const int m = r.m, n = r.n, nb = r.nb;
OUTS:
    for (int ob = 0; ob < nb; ++ob) {
#pragma HLS LOOP_TRIPCOUNT max = 4
        hls::read_lock<typename wf_lanes::group<c_t, L>::type[CGN]> c(c_blk);
        EngineResp resp;
        resp.tx_id = r.tx_id;
        resp.index = ob;
        resp.m = m;
        resp.n = n;
        streamutils::write_msg<WBW>(resp, s_out);
        wf_store_matrix<WBW, L, CGN, c_t, typename IO::c_mem>(c, s_out, m * n);
    }
    EngineStatus st;
    st.tx_id = r.tx_id;
    st.status = r.status;
    streamutils::write_msg<64>(st, m_status);
}

#endif  // WF_SYSTOLIC_ENGINE_STORE_TASK_H
