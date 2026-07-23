#ifndef WAVEFLOW_IL_STORE_INBAND_TASK_H
#define WAVEFLOW_IL_STORE_INBAND_TASK_H
// il_store_inband_task.h — the in-band interleaver's SOB->framed bridge (the second schema-aware stage).
// Reads the descriptor off the middle cmd edge, then frames the writer's stream so the framework
// MemWStream serves the store with no change:
//   MemWCmd{addr=y_off, len=NW, fwd_bursts=1}  -- write Y, buffer the next 1 burst (the response)
//   InterleaverCmd                              -- the response, echoed on s_done after the write commits
//   Y data (NW words)                           -- last=1 on the final word
// read_lock y_blk and stream its NW words as the framed data burst.  No m_axi.  Single-firing.  Its
// pysim twin is IlStoreInband.run_iter (interleaver_inband.py).
//
// !! UNVERIFIED — pending a csynth/XSI run.  Modeled on the verified il_store_task.h (SOB read_lock) +
// !! mem_seq_framed_task.h (write_framed_stream descriptors) + mem_r_stream_framed_task.h
// !! (write_boundary_word framed data).  Needs il_cmd.h emitted with framed methods + mem_w_cmd.h.
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "il_cmd.h"
#include "mem_w_cmd.h"

template <int MEM_DW, int NW>
static void il_store_inband_task(hls::stream<ap_uint<MEM_DW> >& cmd_in,
                                 hls::stream_of_blocks<ap_uint<MEM_DW>[NW] >& y_blk,
                                 hls::stream<streamutils::framed_word<MEM_DW> >& cmd_out) {
    InterleaverCmd c;
    c.read_stream<MEM_DW>(cmd_in);
    // Frame the writer's stream: descriptor (write Y at y_off, NW words), then the echoed response.
    MemWCmd memw;
    memw.addr = c.y_off;
    memw.len = NW;
    memw.fwd_bursts = 1;
    memw.write_framed_stream<MEM_DW>(cmd_out);
    c.write_framed_stream<MEM_DW>(cmd_out);       // response, buffered across the write, echoed on s_done
    // Y data burst — read_lock y_blk and stream its NW words, last on the final word.
    hls::read_lock<ap_uint<MEM_DW>[NW] > yb(y_blk);
ST: for (int w = 0; w < NW; ++w) {
#pragma HLS PIPELINE II=1
        streamutils::write_boundary_word<streamutils::framed_word<MEM_DW>, MEM_DW>(
            cmd_out, yb[w], (w == NW - 1));
    }
}

#endif  // WAVEFLOW_IL_STORE_INBAND_TASK_H
