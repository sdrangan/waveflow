#ifndef WAVEFLOW_IL_CMD_RX_FRAMED_TASK_H
#define WAVEFLOW_IL_CMD_RX_FRAMED_TASK_H
// il_cmd_rx_framed_task.h — the in-band interleaver's schema-aware FRAMER (mem_copy's Sequencer role).
// Reads one InterleaverCmd off the boundary word port and frames the reader's command stream as TWO
// reads, so the framework MemRStream serves the gather with no change:
//   MemRCmd{addr=x_off, len=NW, fwd_bursts=1}  -- read X, and relay the next 1 burst (the descriptor)
//   InterleaverCmd                              -- the descriptor, relayed as a HEADER ahead of X data
//   MemRCmd{addr=p_off, len=NW, fwd_bursts=0}  -- read P, relay nothing
// The reader's m_out then reads [InterleaverCmd | X | P].  Only cmd_rx and il_store are schema-aware;
// the mem-streams relay opaquely.  Single-firing (the hls::task runtime re-fires per command); touches
// ONLY streams (no m_axi).  Its pysim twin is CmdRxInband.run_iter (interleaver_inband.py).
//
// !! UNVERIFIED — pending a csynth/XSI run.  Modeled on the verified mem_seq_framed_task.h; the shapes
// !! (read_stream on the boundary, write_framed_stream per descriptor) are the established pattern, but
// !! this body has NOT been through the toolchain.  Two things the codegen wiring must supply:
// !!   * InterleaverCmd emitted with BOTH plain (read_stream/write_stream) and framed
// !!     (read/write_framed_stream) methods — it is the boundary command AND a relayed descriptor.
// !!   * mem_r_cmd.h / mem_w_cmd.h (framework MemRCmd/MemWCmd) in the interleaver's include set.
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "il_cmd.h"
#include "mem_r_cmd.h"

template <int MEM_DW, int NW>
static void il_cmd_rx_framed_task(hls::stream<ap_uint<MEM_DW> >& s_cmd,
                                  hls::stream<streamutils::framed_word<MEM_DW> >& cmd_out) {
    InterleaverCmd c;
    c.read_stream<MEM_DW>(s_cmd);
    // Read 1: X, with the descriptor relayed as a header (fwd_bursts=1). len is in WORDS (NW).
    MemRCmd memr_x;
    memr_x.addr = c.x_off;
    memr_x.len = NW;
    memr_x.fwd_bursts = 1;
    memr_x.write_framed_stream<MEM_DW>(cmd_out);
    c.write_framed_stream<MEM_DW>(cmd_out);        // the descriptor, welded ahead of the X data
    // Read 2: P, no forward.
    MemRCmd memr_p;
    memr_p.addr = c.p_off;
    memr_p.len = NW;
    memr_p.fwd_bursts = 0;
    memr_p.write_framed_stream<MEM_DW>(cmd_out);
}

#endif  // WAVEFLOW_IL_CMD_RX_FRAMED_TASK_H
