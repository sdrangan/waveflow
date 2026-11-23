#ifndef EXAMPLES_CG_POOL_SPLIT_TASK_H
#define EXAMPLES_CG_POOL_SPLIT_TASK_H
// cg_split_task.h -- the pool's last stage (HAND-WRITTEN), the HLS twin of
// examples/cg_pool/cg_pool.py::CgSplit.  ONE FIRING = ONE WORKER FRAME off the arbiter:
//
//   1. the Resp, onto m_out (its own burst);
//   2. X, copied to m_out a word a cycle until its TLAST -- the head-tested count form of the stream
//      blocks' copy loop (ap_uint<8> b; while (b < 1)), the one shape that holds at RTL;
//   3. the Status, onto m_status.
//
// The words of X are moved as they are: the worker packed them by the schema's lane routines.
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "cg_pool_config.h"
#include "resp.h"
#include "status.h"

template <typename TI, typename TO, typename TS>
static void cg_split_task(hls::stream<TI>& s_in, hls::stream<TO>& m_out, hls::stream<TS>& m_status) {
    Resp r;
    streamutils::read_msg<CG_W>(r, s_in);
    streamutils::write_msg<CG_W>(r, m_out);
    ap_uint<8> b = 0;
COPY:
    while (b < 1) {
#pragma HLS PIPELINE II=1
        bool last;
        const ap_uint<CG_W> w = streamutils::read_boundary_word<TI, CG_W>(s_in, last);
        streamutils::write_boundary_word<TO, CG_W>(m_out, w, last);
        if (last) ++b;
    }
    Status st;
    streamutils::read_msg<CG_W>(st, s_in);
    streamutils::write_msg<CG_W>(st, m_status);
}

#endif  // EXAMPLES_CG_POOL_SPLIT_TASK_H
