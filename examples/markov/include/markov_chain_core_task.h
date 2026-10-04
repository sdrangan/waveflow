#ifndef EXAMPLES_MARKOV_CHAIN_CORE_TASK_H
#define EXAMPLES_MARKOV_CHAIN_CORE_TASK_H
// markov_chain_core_task.h -- the chain of examples/markov (HAND-WRITTEN), the HLS twin of
// ChainCore.run_iter.
//
// Per job, off s_u_fwd: one MkvCmd, then n draws packed four to a word.  ONE STEP PER FIRING:
//
//     t0 = u <  p01        (from state 0: go to 1)
//     t1 = u >= p10        (from state 1: stay at 1)
//     x  = x ? t1 : t0
//
// Both compares depend only on u, so what is carried from firing to firing is the select -- which
// is why this pipelines at II=1 despite the chain's recurrence.
//
// The output, on the framed internal FIFO m_x to the in-band memory writer
// (mem_w_stream_framed_done_task): per CHUNK draws, [MemWCmd(addr, len, 0) | x words] with x packed
// eight to a word by the generated uint8 lane routine; then [MemWCmd(0, 0, 1) | MkvResp].  The writer
// stores each chunk and only then forwards the response, so the host's response means "x is stored".
//
// s_u is a credit stream: s_u_crd carries the CUMULATIVE count of words consumed, offered once at
// least CRD_EVERY words are unreported (plans/mm_credit_stream.md D5), with write_nb -- the offer
// never blocks (rule 2), and a refused one is simply retried on a later firing (rule 1: cumulative).
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "mkv_cmd.h"
#include "mkv_resp.h"
#include "mem_w_cmd.h"
#include "uint16_array_utils.h"
#include "uint8_array_utils.h"

template <int DW, int CRD_EVERY>
static void markov_chain_core_task(hls::stream<ap_uint<DW> >& s_u_fwd,
                                   hls::stream<ap_uint<DW> >& s_u_crd,
                                   hls::stream<streamutils::framed_word<DW> >& m_x) {
#pragma HLS PIPELINE II=1
    const int CW = MkvCmd::nwords<DW>();
    const int WC = MemWCmd::nwords<DW>();
    const int RW = MkvResp::nwords<DW>();
    const int OB = (WC > RW) ? WC : RW;
    const int PFU = uint16_array_utils::lane_capacity<DW>();    // draws per input word
    const int PFX = uint8_array_utils::lane_capacity<DW>();     // states per output word
    const int CHUNK = 64;
    enum { HDR = 0, XCMD = 1, SAMP = 2, RCMD = 3, RESP = 4 };
    static ap_uint<DW> cbuf[CW];
    static ap_uint<DW> obuf[OB];
    static uint16_array_utils::value_type ulane[PFU];
    static uint8_array_utils::value_type xlane[PFX];
    static ap_uint<3> state = HDR;
    static ap_uint<3> ci = 0, oi = 0;
    static bool built = false;           // obuf holds the words of the current XCMD/RCMD/RESP
    static ap_uint<3> ui = 0;            // next draw of ulane; 0 = read a new word first
    static ap_uint<4> xi = 0;            // states collected in xlane
    static ap_uint<16> consumed = 0, offered = 0;
    static ap_uint<32> nleft = 0, k0 = 0, ones = 0;
    static ap_uint<8> c = 0, cleft = 0;
    static ap_uint<64> dst = 0;
    static ap_uint<16> tx_id = 0, p01 = 0, p10 = 0;
    static ap_uint<32> n = 0;
    static ap_uint<1> x = 0;
#pragma HLS ARRAY_PARTITION variable=cbuf complete dim=1
#pragma HLS ARRAY_PARTITION variable=obuf complete dim=1
#pragma HLS ARRAY_PARTITION variable=ulane complete dim=1
#pragma HLS ARRAY_PARTITION variable=xlane complete dim=1

    ap_uint<DW> w;
    if (state == HDR) {
        if (s_u_fwd.read_nb(w)) {
            consumed++;
            cbuf[ci] = w;
            if (ci == CW - 1) {
                ci = 0;
                MkvCmd cmd;
                cmd.read_array<DW>(cbuf);
                n = cmd.n; nleft = cmd.n; k0 = 0; ones = 0;
                x = cmd.x0[0];
                dst = cmd.dstaddr; tx_id = cmd.tx_id; p01 = cmd.p01; p10 = cmd.p10;
                state = XCMD;            // an empty job turns to RCMD next firing (timing: no
                                         // compare on a field just read)
            } else {
                ci++;
            }
        }
    } else if (state == XCMD || state == RCMD || state == RESP) {
        // Emit a small message one word per firing: build it on the first firing.
        const int len = (state == RESP) ? RW : WC;
        if (!built && state == XCMD && nleft == 0) {
            state = RCMD;                // the job had no steps, or its last chunk is done
        } else if (!built) {
            if (state == RESP) {
                MkvResp r;
                r.n = n; r.ones = ones; r.tx_id = tx_id;
                r.write_array<DW>(obuf);
            } else {
                MemWCmd m;
                if (state == XCMD) {
                    c = (nleft < CHUNK) ? ap_uint<8>(nleft) : ap_uint<8>(CHUNK);
                    m.addr = (dst + k0) >> 3;                   // a word index: the writer's base is 0
                    m.len = (c + PFX - 1) / PFX;
                    m.fwd_bursts = 0;
                } else {
                    m.addr = 0; m.len = 0; m.fwd_bursts = 1;    // forward the response, write nothing
                }
                m.write_array<DW>(obuf);
            }
            built = true;
            oi = 0;
        } else {
            streamutils::write_boundary_word<streamutils::framed_word<DW>, DW>(m_x, obuf[oi],
                                                                             oi == len - 1);
            if (oi == len - 1) {
                built = false;
                if (state == XCMD) { cleft = c; ui = 0; xi = 0; state = SAMP; }
                else if (state == RCMD) state = RESP;
                else state = HDR;
            } else {
                oi++;
            }
        }
    } else {                             // SAMP
        bool have = (ui != 0);
        if (!have && s_u_fwd.read_nb(w)) {
            consumed++;
            uint16_array_utils::read_array_lane<DW>(&w, ulane, (cleft < PFU) ? (int)cleft : PFU);
            have = true;
        }
        if (have) {
            const ap_uint<16> u = ulane[ui];
            const ap_uint<1> t0 = (u < p01) ? 1 : 0;
            const ap_uint<1> t1 = (u >= p10) ? 1 : 0;
            x = x ? t1 : t0;
            xlane[xi] = x;
            ones += x;
            cleft--;
            ui = (ui == PFU - 1 || cleft == 0) ? ap_uint<3>(0) : ap_uint<3>(ui + 1);
            if (xi == PFX - 1 || cleft == 0) {
                ap_uint<DW> xw;
                uint8_array_utils::write_array_lane<DW>(xlane, &xw, (int)xi + 1);
                streamutils::write_boundary_word<streamutils::framed_word<DW>, DW>(m_x, xw,
                                                                                 cleft == 0);
                xi = 0;
            } else {
                xi++;
            }
            if (cleft == 0) {
                k0 += c;
                nleft -= c;
                state = XCMD;            // XCMD turns to RCMD when nleft is 0
            }
        }
    }

    // -- the credit offer: cumulative, batched, never blocking ------------------------------------
    if (ap_uint<16>(consumed - offered) >= CRD_EVERY) {
        ap_uint<DW> cw = consumed;
        if (s_u_crd.write_nb(cw)) offered = consumed;
    }
}

#endif  // EXAMPLES_MARKOV_CHAIN_CORE_TASK_H
