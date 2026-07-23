#ifndef WAVEFLOW_IL_LOAD_INBAND_TASK_H
#define WAVEFLOW_IL_LOAD_INBAND_TASK_H
// il_load_inband_task.h — the in-band interleaver's stream->SOB bridge.  Reads the framed
// [InterleaverCmd | X | P] the reader emits, forwards the descriptor to the compute, and write-lock-fills
// two resident blocks (x_blk from the X burst, p_blk from the P burst), each in its own write_lock scope
// so the depth-2 ping-pong overlaps the next job's load with this job's compute.  No m_axi.
// Single-firing.  Its pysim twin is IlLoadInband.run_iter (interleaver_inband.py).
//
// !! UNVERIFIED — pending a csynth/XSI run.  Modeled on the verified il_load_task.h (SOB write_lock fill)
// !! + mem_w_stream_framed_done_task.h (read_framed_stream descriptor + read_boundary_word data).  The X
// !! burst arrives first (the reader appends it after the descriptor header), then the P burst.
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "il_cmd.h"

template <int MEM_DW, int NW>
static void il_load_inband_task(hls::stream<streamutils::framed_word<MEM_DW> >& s_in,
                                hls::stream<ap_uint<MEM_DW> >& cmd_out,
                                hls::stream_of_blocks<ap_uint<MEM_DW>[NW] >& p_blk,
                                hls::stream_of_blocks<ap_uint<MEM_DW>[NW] >& x_blk) {
    InterleaverCmd c;
    streamutils::tlast_status tl;
    c.read_framed_stream<MEM_DW>(s_in, tl);      // descriptor (header)
    c.write_stream<MEM_DW>(cmd_out);             // forward to compute
    bool last;
    // X data — the reader's first firing appended it after the descriptor.
    {
        hls::write_lock<ap_uint<MEM_DW>[NW] > xb(x_blk);
    LX: for (int w = 0; w < NW; ++w) {
#pragma HLS PIPELINE II=1
            xb[w] = streamutils::read_boundary_word<streamutils::framed_word<MEM_DW>, MEM_DW>(
                s_in, last);
        }
    }
    // P data — the reader's second firing.
    {
        hls::write_lock<ap_uint<MEM_DW>[NW] > pb(p_blk);
    LP: for (int w = 0; w < NW; ++w) {
#pragma HLS PIPELINE II=1
            pb[w] = streamutils::read_boundary_word<streamutils::framed_word<MEM_DW>, MEM_DW>(
                s_in, last);
        }
    }
}

#endif  // WAVEFLOW_IL_LOAD_INBAND_TASK_H
