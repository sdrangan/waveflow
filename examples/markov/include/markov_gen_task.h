#ifndef EXAMPLES_MARKOV_GEN_TASK_H
#define EXAMPLES_MARKOV_GEN_TASK_H
// markov_gen_task.h -- kernel 1 of examples/markov (HAND-WRITTEN), the HLS twin of MarkovGen.run_iter.
//
// Per job:
//   HDR  reads one MkvCmd (CW words) off s_cmd;
//   FWD  forwards it on m_u_fwd as one write (TLAST on its last word);
//   GEN  draws n uniforms from xorshift32 -- the top 16 bits of each state -- ONE PER FIRING, packs
//        them four to a word with the generated uint16 lane routine, and sends them in writes of
//        CHUNK samples (TLAST on each write's last word).
//
// m_u is a credit stream (plans/mm_credit_stream.md): m_u_fwd forward, m_u_crd the consumer's
// CUMULATIVE count of words consumed.  A write is admitted only when the credit says it fits
// (avail = QDEPTH - RESP_WORDS - (written - acked), masked at 16 bits), and an admitted write runs to
// its end without looking again -- so the forward channel never stalls the bus it crosses.  The
// credit is read with read_nb, one value per firing (bounded: rule 3); cumulative, so the newest wins.
// A write is never longer than QDEPTH - RESP_WORDS - (CRD_EVERY - 1) words, which is what keeps a
// consumer that batches its credit live (CreditStreamMasterIF.max_write).
//
// A free-running hls::task body pipelined at II=1: every firing moves at most one word per stream.
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "mkv_cmd.h"
#include "uint16_array_utils.h"

template <int DW, int QDEPTH, int CRD_EVERY>
static void markov_gen_task(hls::stream<ap_uint<DW> >& s_cmd,
                            hls::stream<streamutils::axi4s_word<DW> >& m_u_fwd,
                            hls::stream<ap_uint<DW> >& m_u_crd) {
#pragma HLS PIPELINE II=1
    const int CW = MkvCmd::nwords<DW>();
    const int PF = uint16_array_utils::lane_capacity<DW>();     // draws per word
    const int CHUNK = 64;                                       // draws per write
    const int RESP_WORDS = 1;
    static_assert((CHUNK + PF - 1) / PF <= QDEPTH - RESP_WORDS - (CRD_EVERY - 1),
                  "a chunk is longer than the credit stream's max_write");
    static_assert(CW <= QDEPTH - RESP_WORDS - (CRD_EVERY - 1), "the header exceeds max_write");
    enum { HDR = 0, FWD = 1, GEN = 2 };
    // Cross-firing state: `static` is what survives re-firing in an hls::task body.
    static ap_uint<DW> cbuf[CW];
    static uint16_array_utils::value_type lane[PF];
    static ap_uint<2> state = HDR;
    static ap_uint<3> ci = 0;
    static ap_uint<3> li = 0;
    static bool open = false;            // a write is admitted and in progress
    static ap_uint<16> written = 0, acked = 0;
    static ap_uint<32> nleft = 0;        // draws left in the job
    static ap_uint<8> cleft = 0;         // draws left in the current write
    static ap_uint<32> s = 1;            // the xorshift32 state
#pragma HLS ARRAY_PARTITION variable=cbuf complete dim=1
#pragma HLS ARRAY_PARTITION variable=lane complete dim=1

    ap_uint<DW> cv;
    if (m_u_crd.read_nb(cv)) acked = cv(15, 0);
    const ap_uint<16> outstanding = written - acked;            // modular: exact below 2^16
    const ap_uint<17> room = ap_uint<17>(QDEPTH - RESP_WORDS) - outstanding;

    if (state == HDR) {
        ap_uint<DW> w;
        if (s_cmd.read_nb(w)) {
            cbuf[ci] = w;
            if (ci == CW - 1) {
                ci = 0;
                MkvCmd c;
                c.read_array<DW>(cbuf);
                nleft = c.n;
                s = (c.seed == 0) ? ap_uint<32>(1) : ap_uint<32>(c.seed);
                state = FWD;
            } else {
                ci++;
            }
        }
    } else if (state == FWD) {
        if (!open && room >= CW) open = true;
        if (open) {
            streamutils::write_boundary_word<streamutils::axi4s_word<DW>, DW>(m_u_fwd, cbuf[ci],
                                                                            ci == CW - 1);
            written++;
            if (ci == CW - 1) {
                ci = 0;
                open = false;
                state = (nleft != 0) ? GEN : HDR;
            } else {
                ci++;
            }
        }
    } else {                             // GEN
        // Admission and drawing are separate firings: deciding a write fits, then drawing its first
        // sample in the same cycle, chains nleft -> chunk size -> cleft -> its decrement -> "write
        // done" (measured 17.4 ns at a 10 ns target).  One cycle per CHUNK draws buys the clock.
        if (!open) {
            const ap_uint<8> c = (nleft < CHUNK) ? ap_uint<8>(nleft) : ap_uint<8>(CHUNK);
            if (room >= (c + PF - 1) / PF) {
                open = true;
                cleft = c;
                li = 0;
            }
        } else {
            ap_uint<32> x = s;
            x ^= x << 13;
            x ^= x >> 17;
            x ^= x << 5;
            s = x;
            lane[li] = x(31, 16);
            cleft--;
            nleft--;
            if (li == PF - 1 || cleft == 0) {
                ap_uint<DW> w;
                uint16_array_utils::write_array_lane<DW>(lane, &w, (int)li + 1);
                streamutils::write_boundary_word<streamutils::axi4s_word<DW>, DW>(m_u_fwd, w,
                                                                                cleft == 0);
                written++;
                li = 0;
                if (cleft == 0) {
                    open = false;
                    if (nleft == 0) state = HDR;
                }
            } else {
                li++;
            }
        }
    }
}

#endif  // EXAMPLES_MARKOV_GEN_TASK_H
