/**
 * gather.h — HLS implementation of Gather component.
 *
 * Reads blocks via SOBIF read_lock (s_in), unpacks them word-by-word,
 * and emits words to the output stream (m_out).
 *
 * Template args: MEM_DW (word bitwidth), BLOCK_N (words per block)
 */
#pragma once

#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>

template<int MEM_DW, int BLOCK_N>
void gather(
    hls::stream_of_blocks<ap_uint<MEM_DW>[BLOCK_N], 2>::read_lock& s_in,
    hls::stream<ap_uint<MEM_DW>>& m_out
) {
    #pragma HLS INTERFACE mode=axis port=s_in
    #pragma HLS INTERFACE mode=axis port=m_out

    while (true) {
        // Acquire a committed block
        auto block = s_in.acquire();

        // Read BLOCK_N words and emit them to the output stream
        for (int i = 0; i < BLOCK_N; ++i) {
            #pragma HLS PIPELINE II=1
            ap_uint<MEM_DW> word = block[i];
            m_out << word;
        }

        // Release the block back to the producer
        s_in.release();
    }
}
