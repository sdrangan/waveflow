#ifndef WAVEFLOW_FIR_CMD_RX_TASK_H
#define WAVEFLOW_FIR_CMD_RX_TASK_H
// fir_cmd_rx_task.h — the block FIR's schema-aware FRAMER (mem_copy's Sequencer role).  Reads one
// FirCmd off the boundary word port (the ONLY plain stream) and frames the reader's command stream as
// ONE read to the transactional MemRStream:
//   MemRCmd{addr=src_off, len=n, fwd_bursts=1}  -- read n words, relay the next 1 burst (the descriptor)
//   FirDesc{op, n, dst_off, zero_state, tx_id}  -- the descriptor, relayed as a HEADER ahead of the data
// The reader's m_out then carries [FirDesc | data], and fir_compute dispatches on FirDesc.op.
//
// ONE read for BOTH opcodes, deliberately: LOAD_TAPS fetches n coefficients and FILTER fetches an
// n-sample block, so the framing is opcode-independent and the no-output opcode needs no special path.
// That uniformity is what keeps the LOAD_TAPS job from being the odd one out in the token flow -- see
// fir_compute_task.h, where the write side stays uniform too.
//
// len == n because transport is ONE SAMPLE PER 32-BIT WORD whatever the sample width is (see
// examples/fir_block/fir_block.py).  There is no ceil(n/LW) here precisely because there is no packing;
// contrast il_cmd_rx_framed_task.h, which packs LW elements per word and must convert.
//
// Single-firing (the hls::task runtime re-fires per command); touches only streams.  Its pysim twin is
// FirCmdRx.run_iter (examples/fir_block/fir_block.py).
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "fir_cmd.h"
#include "fir_desc.h"
#include "mem_r_cmd.h"

template <int MEM_DW>
static void fir_cmd_rx_task(hls::stream<ap_uint<MEM_DW> >& s_cmd,
                            hls::stream<streamutils::framed_word<MEM_DW> >& cmd_out) {
    FirCmd c;
    c.read_stream<MEM_DW>(s_cmd);
    // The read: n words at src_off, relaying the descriptor as a header (fwd_bursts=1).
    MemRCmd memr;
    memr.addr = c.src_off;
    memr.len = c.n;
    memr.fwd_bursts = 1;
    memr.write_framed_stream<MEM_DW>(cmd_out);
    // The descriptor, welded ahead of the data so a command can never pair with the wrong burst.
    FirDesc d;
    d.op = c.op;
    d.n = c.n;
    d.dst_off = c.dst_off;
    d.zero_state = c.zero_state;
    d.tx_id = c.tx_id;
    d.write_framed_stream<MEM_DW>(cmd_out);
}

#endif  // WAVEFLOW_FIR_CMD_RX_TASK_H
