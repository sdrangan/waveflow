#ifndef WAVEFLOW_IL_CMD_RX_FRAMED_TASK_H
#define WAVEFLOW_IL_CMD_RX_FRAMED_TASK_H
// il_cmd_rx_framed_task.h — the in-band interleaver's schema-aware FRAMER (mem_copy's Sequencer role).
// Reads one InterleaverCmd off the boundary word port (the ONLY plain stream) and frames the reader's
// command stream as TWO reads, so the framework MemRStream serves the gather with no change:
//   MemRCmd{addr=x_off, len=nw, fwd_bursts=1}  -- read X, relay the next 1 burst (the descriptor)
//   IlDesc{n, y_off}                            -- the framed descriptor, relayed as a HEADER ahead of X
//   MemRCmd{addr=p_off, len=nw, fwd_bursts=0}  -- read P, relay nothing
// The reader's m_out then reads [IlDesc | X | P].  nw = ceil(n/LW) is RUNTIME (variable length) — the
// RTL is scenario-independent.  Only cmd_rx and il_store are schema-aware; the mem-streams relay
// opaquely.  Single-firing.  Its pysim twin is CmdRxInband.run_iter (interleaver_inband.py).
//
// csynth-verified (ap_ctrl_none free-running top, Fmax ~111 MHz).  XSI next.
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "il_cmd.h"
#include "il_desc.h"
#include "mem_r_cmd.h"

template <int MEM_DW>
static void il_cmd_rx_framed_task(hls::stream<ap_uint<MEM_DW> >& s_cmd,
                                  hls::stream<streamutils::framed_word<MEM_DW> >& cmd_out) {
    const int LW = MEM_DW / 32;
    InterleaverCmd c;
    c.read_stream<MEM_DW>(s_cmd);
    const int n = (int)c.n;
    const int nw = (n + LW - 1) / LW;              // runtime word count
    // Read 1: X, with the descriptor relayed as a header (fwd_bursts=1).
    MemRCmd memr_x;
    memr_x.addr = c.x_off;
    memr_x.len = nw;
    memr_x.fwd_bursts = 1;
    memr_x.write_framed_stream<MEM_DW>(cmd_out);
    IlDesc d;
    d.n = c.n;
    d.y_off = c.y_off;
    d.write_framed_stream<MEM_DW>(cmd_out);        // the descriptor, welded ahead of the X data
    // Read 2: P, no forward.
    MemRCmd memr_p;
    memr_p.addr = c.p_off;
    memr_p.len = nw;
    memr_p.fwd_bursts = 0;
    memr_p.write_framed_stream<MEM_DW>(cmd_out);
}

#endif  // WAVEFLOW_IL_CMD_RX_FRAMED_TASK_H
