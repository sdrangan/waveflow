/**
 * fill.h — HLS implementation of Fill component.
 *
 * Reads words from s_in stream, gathers block_n words into a block,
 * and commits the block via the SOBIF write_lock (m_out).
 *
 * Template args: MEM_DW (word bitwidth), BLOCK_N (words per block)
 */
#pragma once

#include "hls_stream.h"
#include "hls_streamofblocks.h"
#include <ap_int.h>

template<int MEM_DW, int BLOCK_N>
void fill(
    hls::stream<ap_uint<MEM_DW>>& s_in,
    hls::stream_of_blocks<ap_uint<MEM_DW>[BLOCK_N], 2>::write_lock& m_out
) {
    #pragma HLS INTERFACE mode=axis port=s_in
    #pragma HLS INTERFACE mode=axis port=m_out

    while (true) {
        // Acquire a free block
        auto block = m_out.acquire();

        // Read BLOCK_N words and fill the block
        for (int i = 0; i < BLOCK_N; ++i) {
            #pragma HLS PIPELINE II=1
            ap_uint<MEM_DW> word;
            s_in >> word;
            block[i] = word;
        }

        // Commit the filled block
        m_out.commit();
    }
}
