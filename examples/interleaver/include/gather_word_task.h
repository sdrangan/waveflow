#ifndef WAVEFLOW_BUILD_GATHER_WORD_TASK_H
#define WAVEFLOW_BUILD_GATHER_WORD_TASK_H
// gather_word_task.h — the FIXED word-granular Gather body (the sob3 shape), the generated analogue
// of interleaver_task_sob3.cpp's gather_task (Phase 4).  A pure-AXIS, single-firing hls::task body
// (the runtime re-fires it per job): read-lock the filled WORD block, then per output word read one
// packed index-word (LW indices), do LW random block reads via elem_read<MEM_DW> (#pragma HLS UNROLL;
// LW = MEM_DW/32 = 2 for 64b), and pack the LW gathered 32-bit results into one output word.  This is
// where Phase 3's elem_read<W> (pf=2 conformance) + the dual-port ping-pong (2 arbitrary reads/cycle
// FREE) pay off — the true bus-floor gather.  Copied verbatim by MemStreamStep, instantiated
// <MEM_DW, NW> by the top.  The block element type's array-utils header supplies elem_read.
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
#include "il_elem_array_utils.h"

// p_in (packed indices) x word block -> y_out: y_word lane l = block[ P[w*LW+l] ]. LW outputs/cycle.
template <int MEM_DW, int NW>
static void gather_word_task(hls::stream<ap_uint<MEM_DW> >& p_in,
                             hls::stream_of_blocks<ap_uint<MEM_DW>[NW] >& x_blk,
                             hls::stream<ap_uint<MEM_DW> >& y_out) {
    const int LW = MEM_DW / 32;
    hls::read_lock<ap_uint<MEM_DW>[NW] > b(x_blk);
GY: for (int w = 0; w < NW; ++w) {
#pragma HLS PIPELINE II=1
        ap_uint<MEM_DW> pword = p_in.read();
        ap_uint<MEM_DW> yword = 0;
        for (int l = 0; l < LW; ++l) {
#pragma HLS UNROLL
            int idx = (int)pword.range(32 * l + 31, 32 * l);          // output elem index P[w*LW+l]
            ap_uint<32> xv = (ap_uint<32>)il_elem_array_utils::elem_read<MEM_DW>(&b[0], idx);
            yword.range(32 * l + 31, 32 * l) = xv;                    // pack LW results into one word
        }
        y_out.write(yword);
    }
}

#endif  // WAVEFLOW_BUILD_GATHER_WORD_TASK_H
