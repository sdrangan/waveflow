// cg_mm_rx_task.h -- CgMmUnit's framer: one CgMmUnitCmd -> 1 + nit reads for the MemRStream.
//   MemRCmd{a_off, NWA, fwd=1} | CgDesc{nit, out_off} | MemRCmd{p_off + n*NWP, NWP, fwd=0} for n < nit
// NWA / NWP words hold A (K x K) / one P (K x N).  Requires nit <= K.
// Python twin: CgMmRx.run_iter (examples/mimo_cg/hw/mm.py).
#ifndef MIMO_CG_MM_RX_TASK_H
#define MIMO_CG_MM_RX_TASK_H
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "cg_mm_unit_cmd.h"
#include "cg_desc.h"
#include "mem_r_cmd.h"
#include "cg_types.h"

template <int MEM_DW, int K, int N>
static void cg_mm_rx_task(hls::stream<ap_uint<MEM_DW> >& s_cmd,
                          hls::stream<streamutils::framed_word<MEM_DW> >& cmd_out) {
    const int LW = cg::a_mem::lane_capacity<MEM_DW>();
    const int NWA = (K * K + LW - 1) / LW;
    const int NWP = (K * N + LW - 1) / LW;
    CgMmUnitCmd c;
    c.read_stream<MEM_DW>(s_cmd);
    // nit is clamped to 1..K: the blocks' loops end at K, so a larger nit would never send LAST.
    const int nit = (int)c.nit < 1 ? 1 : ((int)c.nit > K ? K : (int)c.nit);
    MemRCmd ra;
    ra.addr = c.a_off;
    ra.len = NWA;
    ra.fwd_bursts = 1;
    ra.write_framed_stream<MEM_DW>(cmd_out);
    CgDesc d;
    d.nit = nit;
    d.x_off = c.out_off;
    d.write_framed_stream<MEM_DW>(cmd_out);
READS:
    for (int n = 0; n < K; ++n) {
        if (n < nit) {
            MemRCmd rp;
            rp.addr = c.p_off + n * NWP;
            rp.len = NWP;
            rp.fwd_bursts = 0;
            rp.write_framed_stream<MEM_DW>(cmd_out);
        }
    }
}

#endif  // MIMO_CG_MM_RX_TASK_H
