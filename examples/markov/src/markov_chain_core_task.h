#ifndef EXAMPLES_MARKOV_CHAIN_CORE_TASK_H
#define EXAMPLES_MARKOV_CHAIN_CORE_TASK_H
// markov_chain_core_task.h -- the chain of examples/markov (HAND-WRITTEN), the HLS twin of
// ChainCore.run_iter.
//
// ONE FIRING = ONE JOB, written as the Python reads (docs/guide/patterns/command_response.md):
//
//   1. read the forwarded MkvCmd off s_u_fwd;
//   2. per chunk of CHUNK steps: a MemWCmd(addr, len, 0) for the in-band memory writer, then a
//      pipelined loop of ONE STEP PER CYCLE -- a word of draws read every four steps, the states
//      packed eight to a word (the lane loop, one element per iteration;
//      docs/guide/vectorization/hls/loop_optimization.md):
//
//          t0 = u <  p01        (from state 0: go to 1)
//          t1 = u >= p10        (from state 1: stay at 1)
//          x  = x ? t1 : t0
//
//      Both compares depend only on u, so the dependency carried from step to step is the select --
//      which is why the loop runs at II=1 despite the recurrence;
//   3. MemWCmd(0, 0, 1) then the MkvResp: the writer forwards the response once x is stored.
//
// s_u is a credit stream: s_u_crd carries the CUMULATIVE count of words consumed, offered once at
// least CRD_EVERY words are unreported -- checked after each chunk, by the framework's
// credit::Consumer (credit_stream_hls.h).  The offer is a blocking write:
// what reads it (the credit bus writer) coalesces and never waits on the bus (a credit-in write is
// always taken), so it cannot hold the chain up -- and a dropped offer could leave a producer that is
// waiting for exactly that credit waiting forever, since nothing more would arrive to prompt another.
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "mkv_cmd.h"
#include "mkv_resp.h"
#include "mem_w_cmd.h"
#include "uint16_array_utils.h"
#include "uint8_array_utils.h"
#include "credit_stream_hls.h"

template <int DW, int CRD_EVERY>
static void markov_chain_core_task(hls::stream<ap_uint<DW> >& s_u_fwd,
                                   hls::stream<ap_uint<DW> >& s_u_crd,
                                   hls::stream<streamutils::framed_word<DW> >& m_x) {
    const int CW = MkvCmd::nwords<DW>();
    const int PFU = uint16_array_utils::lane_capacity<DW>();    // draws per input word
    const int PFX = uint8_array_utils::lane_capacity<DW>();     // states per output word
    const int CHUNK = 64;
    static credit::Consumer<CRD_EVERY> crd;                     // survives from job to job

    // 1. the command
    MkvCmd cmd;
    cmd.read_stream<DW>(s_u_fwd);
    crd.took(CW);
    const ap_uint<32> n = cmd.n;
    const ap_uint<16> p01 = cmd.p01, p10 = cmd.p10;
    ap_uint<1> x = cmd.x0[0];
    ap_uint<32> ones = 0;

    // 2. the steps, a chunk per memory write
    uint16_array_utils::value_type ulane[PFU];
    uint8_array_utils::value_type xlane[PFX];
#pragma HLS ARRAY_PARTITION variable=ulane complete dim=1
#pragma HLS ARRAY_PARTITION variable=xlane complete dim=1
CHUNKS: for (ap_uint<32> k0 = 0; k0 < n; k0 += CHUNK) {
        const ap_uint<32> rem = n - k0;
        const int c = (rem < CHUNK) ? (int)rem : CHUNK;
        MemWCmd m;
        m.addr = (cmd.dstaddr + k0) >> 3;                       // a word index: the writer's base is 0
        m.len = (c + PFX - 1) / PFX;
        m.fwd_bursts = 0;
        m.write_framed_stream<DW>(m_x);
    STEP: for (int k = 0; k < c; ++k) {
#pragma HLS PIPELINE II=1
            const int ju = k % PFU;
            if (ju == 0) {                                      // a fresh word of draws
                ap_uint<DW> w = s_u_fwd.read();
                crd.took(1);
                uint16_array_utils::read_array_lane<DW>(&w, ulane, (c - k < PFU) ? c - k : PFU);
            }
            const ap_uint<16> u = ulane[ju];
            const ap_uint<1> t0 = (u < p01) ? 1 : 0;
            const ap_uint<1> t1 = (u >= p10) ? 1 : 0;
            x = x ? t1 : t0;
            ones += x;
            const int jx = k % PFX;
            xlane[jx] = x;
            if (jx == PFX - 1 || k == c - 1) {                  // a word of states is full
                ap_uint<DW> xw;
                uint8_array_utils::write_array_lane<DW>(xlane, &xw, jx + 1);
                streamutils::write_boundary_word<streamutils::framed_word<DW>, DW>(m_x, xw,
                                                                                 k == c - 1);
            }
        }
        crd.offer(s_u_crd);                      // report: between chunks
    }
    crd.offer(s_u_crd);

    // 3. the response, forwarded by the writer once x is stored
    MemWCmd fin;
    fin.addr = 0; fin.len = 0; fin.fwd_bursts = 1;
    fin.write_framed_stream<DW>(m_x);
    MkvResp r;
    r.n = n; r.ones = ones; r.tx_id = cmd.tx_id;
    r.write_framed_stream<DW>(m_x);
}

#endif  // EXAMPLES_MARKOV_CHAIN_CORE_TASK_H
