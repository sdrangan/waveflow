/**
 * gather_toy_top.cpp — Gather_toy top-level module for Vitis HLS csynth.
 *
 * Composite: InputDriver (StreamIF) → Fill (SOBIF write_lock) →
 *            SOBIF (ping-pong) → Gather (SOBIF read_lock) → OutputCapture (StreamIF)
 *
 * This is the canonical gather_toy design: minimal proof-of-concept for typed SOBIF.
 * - MEM_DW=64: 64-bit words
 * - BLOCK_N=8: 8 words per block (512 bits)
 * - Dataflow: free-running pipeline with per-job token pacing
 *
 * Expected steady-state: ~1301 cycles (from sob_task baseline)
 */

#pragma once
#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>
#include "fill.h"
#include "gather.h"

#define MEM_DW 64
#define BLOCK_N 8

void gather_toy_top(
    // Input boundary (AXIS)
    hls::stream<ap_uint<MEM_DW>>& s_in,
    // Output boundary (AXIS)
    hls::stream<ap_uint<MEM_DW>>& m_out
) {
    #pragma HLS INTERFACE mode=axis port=s_in
    #pragma HLS INTERFACE mode=axis port=m_out
    #pragma HLS DATAFLOW

    // Internal SOBIF channel (ping-pong buffer for typed blocks)
    hls::stream_of_blocks<ap_uint<MEM_DW>[BLOCK_N], 2> sob_channel;

    // Three-stage dataflow:
    // 1. Fill: read words from s_in, write blocks to SOBIF
    fill<MEM_DW, BLOCK_N>(s_in, sob_channel.write_lock());

    // 2. SOBIF: transparent ping-pong buffering (hls::stream_of_blocks handles this)

    // 3. Gather: read blocks from SOBIF, emit words to m_out
    gather<MEM_DW, BLOCK_N>(sob_channel.read_lock(), m_out);
}
