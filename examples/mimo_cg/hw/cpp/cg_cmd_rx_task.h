// cg_cmd_rx_task.h -- the detector's framer: one CgCmd -> two reads for the MemRStream.
//   MemRCmd{a_off, NWA, fwd=1} | CgDesc{nit, x_off} | MemRCmd{b_off, NWB, fwd=0}
// Python twin: CgCmdRx.run_iter (examples/mimo_cg/hw/detector.py).
#ifndef MIMO_CG_CMD_RX_TASK_H
#define MIMO_CG_CMD_RX_TASK_H
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "cg_cmd.h"
#include "cg_desc.h"
#include "mem_r_cmd.h"
#include "cg_types.h"

template <int MEM_DW, int K, int N>
static void cg_cmd_rx_task(hls::stream<ap_uint<MEM_DW> >& s_cmd,
                           hls::stream<streamutils::framed_word<MEM_DW> >& cmd_out) {
    const int LW = cg::a_mem::lane_capacity<MEM_DW>();
    CgCmd c;
    c.read_stream<MEM_DW>(s_cmd);
    // nit is clamped to 1..K: the blocks' loops end at K, so a larger nit would never send LAST.
    const int nit = (int)c.nit < 1 ? 1 : ((int)c.nit > K ? K : (int)c.nit);
    MemRCmd ra;
    ra.addr = c.a_off;
    ra.len = (K * K + LW - 1) / LW;
    ra.fwd_bursts = 1;
    ra.write_framed_stream<MEM_DW>(cmd_out);
    CgDesc d;
    d.nit = nit;
    d.x_off = c.x_off;
    d.write_framed_stream<MEM_DW>(cmd_out);
    MemRCmd rb;
    rb.addr = c.b_off;
    rb.len = (K * N + LW - 1) / LW;
    rb.fwd_bursts = 0;
    rb.write_framed_stream<MEM_DW>(cmd_out);
}

#endif  // MIMO_CG_CMD_RX_TASK_H
