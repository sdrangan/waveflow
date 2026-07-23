#ifndef WAVEFLOW_IL_LOAD_INBAND_TASK_H
#define WAVEFLOW_IL_LOAD_INBAND_TASK_H
// il_load_inband_task.h — the in-band interleaver's stream->SOB bridge.  Reads the framed
// [IlDesc | X | P] the reader emits, forwards the framed descriptor to the compute, and write-lock-fills
// two resident blocks (x_blk from the X burst, p_blk from the P burst) — each in its own write_lock
// scope so the depth-2 ping-pong overlaps the next job's load with this job's compute.  nw = ceil(n/LW)
// is RUNTIME (variable length); the block is sized NW (the max).  No m_axi.  Single-firing.  Its pysim
// twin is IlLoadInband.run_iter (interleaver_inband.py).
//
// !! UNVERIFIED — pending a csynth/XSI run.  Modeled on il_load_task.h (SOB write_lock fill) +
// !! mem_w_stream_framed_done_task.h (read_framed_stream descriptor + read_boundary_word data).
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "il_desc.h"

template <int MEM_DW, int NW>
static void il_load_inband_task(hls::stream<streamutils::framed_word<MEM_DW> >& s_in,
                                hls::stream<streamutils::framed_word<MEM_DW> >& desc_out,
                                hls::stream_of_blocks<ap_uint<MEM_DW>[NW] >& p_blk,
                                hls::stream_of_blocks<ap_uint<MEM_DW>[NW] >& x_blk) {
    const int LW = MEM_DW / 32;
    IlDesc d;
    streamutils::tlast_status tl;
    d.read_framed_stream<MEM_DW>(s_in, tl);          // descriptor (header)
    d.write_framed_stream<MEM_DW>(desc_out);         // forward to compute
    const int nw = ((int)d.n + LW - 1) / LW;
    bool last;
    // X data — the reader's first firing appended it after the descriptor.
    {
        hls::write_lock<ap_uint<MEM_DW>[NW] > xb(x_blk);
    LX: for (int w = 0; w < nw; ++w) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT max=NW
            xb[w] = streamutils::read_boundary_word<streamutils::framed_word<MEM_DW>, MEM_DW>(
                s_in, last);
        }
    }
    // P data — the reader's second firing.
    {
        hls::write_lock<ap_uint<MEM_DW>[NW] > pb(p_blk);
    LP: for (int w = 0; w < nw; ++w) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT max=NW
            pb[w] = streamutils::read_boundary_word<streamutils::framed_word<MEM_DW>, MEM_DW>(
                s_in, last);
        }
    }
}

#endif  // WAVEFLOW_IL_LOAD_INBAND_TASK_H
