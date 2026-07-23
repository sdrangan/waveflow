#ifndef WAVEFLOW_IL_CMD_RX_FRAMED_TASK_H
#define WAVEFLOW_IL_CMD_RX_FRAMED_TASK_H
// il_cmd_rx_framed_task.h — the in-band interleaver's schema-aware FRAMER (mem_copy's Sequencer role).
// Reads one InterleaverCmd off the boundary word port (the ONLY plain stream) and frames the reader's
// command stream as ONE read of the contiguous [P | X] region, so the framework MemRStream serves the
// gather with no change:
//   MemRCmd{addr=p_off, len=2*nw, fwd_bursts=1}  -- read P then X (contiguous), relay the next 1 burst
//   IlDesc{n, y_off}                             -- the framed descriptor, relayed as a HEADER
// The reader's m_out then reads [IlDesc | P | X], and il_load fills p_blk from the first nw words then
// x_blk from the next nw.  ONE read (not two) is deliberate: MemRStream is an hls::task reading one
// region per firing, and a SECOND firing for X WEDGES the free-running pipeline — the reader never
// issues its 2nd m_axi read while il_load holds a stream_of_blocks write-lock across both firings
// (traced via XSI; block-order and FIFO-depth fixes did not help).  Reading the contiguous P|X region
// in one burst is the fix (x_off must == p_off + nw).  nw = ceil(n/LW) is RUNTIME (variable length) —
// the RTL is scenario-independent.  Only cmd_rx and il_store are schema-aware; the mem-streams relay
// opaquely.  Single-firing.  Its pysim twin is CmdRxInband.run_iter (interleaver_inband.py).
//
// csynth- + XSI-verified (ap_ctrl_none free-running top).
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
    const int nw = (n + LW - 1) / LW;              // runtime word count per region
    // ONE read of the contiguous [P | X] region (2*nw words; X sits right after P), descriptor as a
    // header.  A single reader firing on purpose (see the header comment): a second firing for X wedges
    // the free-running pipeline.
    MemRCmd memr;
    memr.addr = c.p_off;
    memr.len = 2 * nw;
    memr.fwd_bursts = 1;
    memr.write_framed_stream<MEM_DW>(cmd_out);
    IlDesc d;
    d.n = c.n;
    d.y_off = c.y_off;
    d.write_framed_stream<MEM_DW>(cmd_out);        // the descriptor, welded ahead of the P|X data
}

#endif  // WAVEFLOW_IL_CMD_RX_FRAMED_TASK_H
