// cg_vec_rx_task.h -- CgVecUnit's framer: one CgVecUnitCmd -> 1 + nit reads for the MemRStream.
//   MemRCmd{b_off, NW, fwd=1} | CgDesc{nit, out_off} | MemRCmd{s_off + n*NW, NW, fwd=0} for n < nit
// NW words hold one K x N matrix (lane_capacity complex elements per word).  Requires nit <= K.
// Python twin: CgVecRx.run_iter (examples/mimo_cg/hw/vec.py).
#ifndef MIMO_CG_VEC_RX_TASK_H
#define MIMO_CG_VEC_RX_TASK_H
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "cg_vec_unit_cmd.h"
#include "cg_desc.h"
#include "mem_r_cmd.h"
#include "cg_types.h"

template <int MEM_DW, int K, int N>
static void cg_vec_rx_task(hls::stream<ap_uint<MEM_DW> >& s_cmd,
                           hls::stream<streamutils::framed_word<MEM_DW> >& cmd_out) {
    const int LW = cg::b_mem::lane_capacity<MEM_DW>();
    const int NW = (K * N + LW - 1) / LW;
    CgVecUnitCmd c;
    c.read_stream<MEM_DW>(s_cmd);
    // nit is clamped to 1..K: the blocks' loops end at K, so a larger nit would never send LAST.
    const int nit = (int)c.nit < 1 ? 1 : ((int)c.nit > K ? K : (int)c.nit);
    MemRCmd rb;
    rb.addr = c.b_off;
    rb.len = NW;
    rb.fwd_bursts = 1;
    rb.write_framed_stream<MEM_DW>(cmd_out);
    CgDesc d;
    d.nit = nit;
    d.x_off = c.out_off;
    d.write_framed_stream<MEM_DW>(cmd_out);
READS:
    for (int n = 0; n < K; ++n) {
        if (n < nit) {
            MemRCmd rs;
            rs.addr = c.s_off + n * NW;
            rs.len = NW;
            rs.fwd_bursts = 0;
            rs.write_framed_stream<MEM_DW>(cmd_out);
        }
    }
}

#endif  // MIMO_CG_VEC_RX_TASK_H
