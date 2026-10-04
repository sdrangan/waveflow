#ifndef WAVEFLOW_IL_STORE_INBAND_TASK_H
#define WAVEFLOW_IL_STORE_INBAND_TASK_H
// il_store_inband_task.h — the in-band interleaver's SOB->framed bridge, TYPED-block edition.  Reads the
// framed IlDesc, frames the writer's command, then SERIALIZES the 32-bit ELEMENT block y_blk back into
// framed words via the generated write_framed_stream_lane so the framework MemWStream serves the store
// unchanged:
//   MemWCmd{addr=y_off, len=nw, fwd_bursts=1}   -- write Y (nw words), buffer the response (1 burst)
//   IlDesc{n, y_off}                            -- the response, echoed on s_done after the write commits
//   Y data (nw words, last on the final word)   -- LW elements per beat from write_framed_stream_lane
// n runtime; nw = ceil(n/LW).  Block is ap_uint<32>[N] (element granularity).  No m_axi.  Single-firing.
// Its pysim twin is IlStoreInband.run_iter (interleaver_inband.py).
//
// csynth- + XSI-verified.
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "il_desc.h"
#include "mem_w_cmd.h"
#include "il_elem_array_utils.h"

template <int MEM_DW, int N>
static void il_store_inband_task(hls::stream<streamutils::framed_word<MEM_DW> >& desc_in,
                                 hls::stream_of_blocks<ap_uint<32>[N] >& y_blk,
                                 hls::stream<streamutils::framed_word<MEM_DW> >& cmd_out) {
    const int LW = il_elem_array_utils::lane_capacity<MEM_DW>();
    IlDesc d;
    streamutils::tlast_status tl;
    d.read_framed_stream<MEM_DW>(desc_in, tl);
    const int n = (int)d.n;
    const int nw = (n + LW - 1) / LW;
    // Frame the writer's stream: descriptor (write Y at y_off, nw words), then the echoed response.
    MemWCmd memw;
    memw.addr = d.y_off;
    memw.len = nw;
    memw.fwd_bursts = 1;
    memw.write_framed_stream<MEM_DW>(cmd_out);
    d.write_framed_stream<MEM_DW>(cmd_out);         // response, buffered across the write, echoed on s_done
    // Serialize the n gathered elements -> nw framed words, LW elements per beat, tlast on the final word.
    hls::read_lock<ap_uint<32>[N] > yb(y_blk);
ST: for (int i = 0; i < n; i += LW) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT max=N
        il_elem_array_utils::write_framed_stream_lane<MEM_DW>(&yb[i], cmd_out, (i + LW >= n), LW);
    }
}

#endif  // WAVEFLOW_IL_STORE_INBAND_TASK_H
