#ifndef EXAMPLES_MARKOV_GEN_TASK_H
#define EXAMPLES_MARKOV_GEN_TASK_H
// markov_gen_task.h -- kernel 1 of examples/markov (HAND-WRITTEN), the HLS twin of MarkovGen.run_iter.
//
// ONE FIRING = ONE JOB, written as the Python reads (the command-response pattern,
// docs/guide/patterns/command_response.md):
//
//   1. read the MkvCmd off s_cmd;
//   2. forward it on m_u_fwd, as one write (TLAST on its last word);
//   3. draw n uniforms from xorshift32 -- the top 16 bits of each state -- in writes of CHUNK draws:
//      per chunk, wait for the credit, then a pipelined loop of ONE DRAW PER CYCLE, packing four draws
//      to a word with the generated uint16 lane routine (the lane loop, one element per iteration --
//      docs/guide/vectorization/hls/loop_optimization.md), TLAST on the chunk's last word.
//
// m_u is a credit stream (plans/mm_credit_stream.md): m_u_fwd forward, m_u_crd the consumer's
// CUMULATIVE count of words consumed.  Before each write the body waits until the write fits --
// QDEPTH - RESP_WORDS - (written - acked) >= its words, masked at 16 bits -- reading the credit stream
// (blocking, then a bounded drain to the newest value).  An admitted write runs to its end without
// looking again, so the forward channel never stalls the bus it crosses.  Credit is also drained
// (bounded, non-blocking) at each chunk even when there is room, so the credit FIFO never fills with
// stale values.  A write is never longer than max_write = QDEPTH - RESP_WORDS - (CRD_EVERY - 1).
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "mkv_cmd.h"
#include "uint16_array_utils.h"

template <int DW, int QDEPTH>
static inline void markov_take_credit(hls::stream<ap_uint<DW> >& crd, ap_uint<16>& acked,
                                      ap_uint<16> written, int nwords) {
    const int ROOM = QDEPTH - 1;                     // RESP_WORDS = 1 reserved
    ap_uint<DW> v;
DRAIN0: for (int i = 0; i < 4; ++i) {                // bounded: take what is already there
        if (!crd.read_nb(v)) break;
        acked = v(15, 0);
    }
WAIT: while (ap_uint<16>(written - acked) > ap_uint<16>(ROOM - nwords)) {
        acked = crd.read()(15, 0);                   // sleep until a value arrives ...
    DRAIN: for (int i = 0; i < 4; ++i) {             // ... then catch up to the newest
            if (!crd.read_nb(v)) break;
            acked = v(15, 0);
        }
    }
}

template <int DW, int QDEPTH, int CRD_EVERY>
static void markov_gen_task(hls::stream<ap_uint<DW> >& s_cmd,
                            hls::stream<streamutils::axi4s_word<DW> >& m_u_fwd,
                            hls::stream<ap_uint<DW> >& m_u_crd) {
    const int CW = MkvCmd::nwords<DW>();
    const int PF = uint16_array_utils::lane_capacity<DW>();     // draws per word
    const int CHUNK = 64;                                       // draws per write
    static_assert((CHUNK + PF - 1) / PF <= QDEPTH - 1 - (CRD_EVERY - 1),
                  "a chunk is longer than the credit stream's max_write");
    static_assert(CW <= QDEPTH - 1 - (CRD_EVERY - 1), "the command exceeds max_write");
    static ap_uint<16> written = 0, acked = 0;                  // survive from job to job

    // 1. the command
    MkvCmd cmd;
    cmd.read_stream<DW>(s_cmd);
    const ap_uint<32> n = cmd.n;
    ap_uint<32> s = (cmd.seed == 0) ? ap_uint<32>(1) : ap_uint<32>(cmd.seed);

    // 2. forward it
    markov_take_credit<DW, QDEPTH>(m_u_crd, acked, written, CW);
    cmd.write_axi4_stream<DW>(m_u_fwd);
    written += CW;

    // 3. the draws, a chunk per write, one draw per cycle
    uint16_array_utils::value_type lane[PF];
#pragma HLS ARRAY_PARTITION variable=lane complete dim=1
CHUNKS: for (ap_uint<32> k0 = 0; k0 < n; k0 += CHUNK) {
        const ap_uint<32> rem = n - k0;
        const int c = (rem < CHUNK) ? (int)rem : CHUNK;
        const int cw = (c + PF - 1) / PF;
        markov_take_credit<DW, QDEPTH>(m_u_crd, acked, written, cw);
    GEN: for (int k = 0; k < c; ++k) {
#pragma HLS PIPELINE II=1
            s ^= s << 13;
            s ^= s >> 17;
            s ^= s << 5;
            const int j = k % PF;
            lane[j] = s(31, 16);
            if (j == PF - 1 || k == c - 1) {             // a word is full, or the chunk ends
                ap_uint<DW> w;
                uint16_array_utils::write_array_lane<DW>(lane, &w, j + 1);
                streamutils::write_boundary_word<streamutils::axi4s_word<DW>, DW>(m_u_fwd, w,
                                                                                k == c - 1);
            }
        }
        written += cw;
    }
}

#endif  // EXAMPLES_MARKOV_GEN_TASK_H
