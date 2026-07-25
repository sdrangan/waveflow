#ifndef WAVEFLOW_IL_LOAD_INBAND_TASK_H
#define WAVEFLOW_IL_LOAD_INBAND_TASK_H
// il_load_inband_task.h — the in-band interleaver's stream->SOB bridge, TYPED-block edition.  Reads the
// framed [IlDesc | P | X] the reader emits, forwards the descriptor, and DESERIALIZES each burst into a
// 32-bit ELEMENT block (p_blk from P, x_blk from X) via the generated read_framed_stream_lane — so the
// packing lives here, at the stream<->block boundary, and il_compute sees a plain typed array it indexes
// directly (see guide/vectorization/hls/arrayutils.md).  Blocks are ap_uint<32>[N] (N = element count,
// the SOBIF element_type); LW = lane_capacity<MEM_DW>() = MEM_DW/32 elements per beat.  P filled FIRST so
// p_blk releases before x_blk (matches il_compute's read-lock order).  No m_axi.  Single-firing.  Its
// pysim twin is IlLoadInband.run_iter (interleaver_inband.py).
//
// csynth- + XSI-verified.
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "il_desc.h"
#include "il_elem_array_utils.h"

template <int MEM_DW, int N>
static void il_load_inband_task(hls::stream<streamutils::framed_word<MEM_DW> >& s_in,
                                hls::stream<streamutils::framed_word<MEM_DW> >& desc_out,
                                hls::stream_of_blocks<ap_uint<32>[N] >& p_blk,
                                hls::stream_of_blocks<ap_uint<32>[N] >& x_blk) {
    const int LW = il_elem_array_utils::lane_capacity<MEM_DW>();
    IlDesc d;
    streamutils::tlast_status tl;
    d.read_framed_stream<MEM_DW>(s_in, tl);          // descriptor (header)
    d.write_framed_stream<MEM_DW>(desc_out);         // forward to compute
    const int n = (int)d.n;
    // P data — deserialize the burst into the element block, LW elements per beat.  Filled FIRST.
    {
        hls::write_lock<ap_uint<32>[N] > pb(p_blk);
    LP: for (int i = 0; i < n; i += LW) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT max=N
            il_elem_array_utils::read_framed_stream_lane<MEM_DW>(s_in, &pb[i], LW, tl);
        }
    }
    // X data — the reader's second burst.
    {
        hls::write_lock<ap_uint<32>[N] > xb(x_blk);
    LX: for (int i = 0; i < n; i += LW) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT max=N
            il_elem_array_utils::read_framed_stream_lane<MEM_DW>(s_in, &xb[i], LW, tl);
        }
    }
}

#endif  // WAVEFLOW_IL_LOAD_INBAND_TASK_H
