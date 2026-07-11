#ifndef WAVEFLOW_BUILD_SPLIT_FILL_TASK_H
#define WAVEFLOW_BUILD_SPLIT_FILL_TASK_H
// split_fill_task.h — the FIXED SplitFill body for the P-SOB interleaver variant (the symmetric
// topology where BOTH P and X are resident SOB blocks).  Merges the stream-mix variant's Demux + Fill
// into one pure-AXIS, single-firing hls::task body (the runtime re-fires it per job; NO m_axi here —
// DTLP stays clean): read the MemRStream mem_in run (P words then X words, NW each) and write-lock-fill
// two resident blocks — p_blk (first NW words) then x_blk (next NW).  Each block is filled in its own
// write_lock scope (one lock held at a time; the '}' frees it so the consumer can read-lock it and the
// depth-2 ping-pong overlaps).  Copied verbatim by MemStreamStep, instantiated <MEM_DW, NW> by the top.
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>

// mem_in (P words then X words) -> two resident blocks: p_blk (first NW) + x_blk (next NW). Word rate.
template <int MEM_DW, int NW>
static void split_fill_task(hls::stream<ap_uint<MEM_DW> >& mem_in,
                            hls::stream_of_blocks<ap_uint<MEM_DW>[NW] >& p_blk,
                            hls::stream_of_blocks<ap_uint<MEM_DW>[NW] >& x_blk) {
    {
        hls::write_lock<ap_uint<MEM_DW>[NW] > pb(p_blk);
    SFP: for (int w = 0; w < NW; ++w) {
#pragma HLS PIPELINE II=1
            pb[w] = mem_in.read();
        }
    }
    {
        hls::write_lock<ap_uint<MEM_DW>[NW] > xb(x_blk);
    SFX: for (int w = 0; w < NW; ++w) {
#pragma HLS PIPELINE II=1
            xb[w] = mem_in.read();
        }
    }
}

#endif  // WAVEFLOW_BUILD_SPLIT_FILL_TASK_H
