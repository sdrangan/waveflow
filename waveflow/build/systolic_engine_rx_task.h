// systolic_engine_rx_task.h -- the systolic engine's receiver: one command per firing.
//
// Reads an EngineCmd, validates it (the operation: BAD_OP; the dimensions against the maxima and the
// array, and keep_a only with one panel: BAD_DIMS), sends the job to both loaders and the core and the
// reply to the store, then forwards the operand bursts in order -- A bursts to one loader, B bursts
// to the other -- a word a cycle.  With keep_a the payload is A then the nb matrices B; otherwise, per
// output and per panel, A then B.  A refused command is answered (its status, no outputs) and is
// FATAL: the receiver stops taking input until a reset.  IN_T is s_in's word type (KernelTask.framed).
// Python twin: waveflow.linalg.systolic_engine.EngineRx.
#ifndef WF_SYSTOLIC_ENGINE_RX_TASK_H
#define WF_SYSTOLIC_ENGINE_RX_TASK_H
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "wf_linalg_msg.h"
#include "wf_linalg_traits.h"
#include "wf_engine_cmd.h"
#include "wf_engine_job.h"
#include "wf_engine_reply.h"

template <int WBW, int MMAX, int KMAX, int NMAX, int PMAX, int L, int R, int C, int IOID,
          typename IN_T>
static void systolic_engine_rx_task(hls::stream<IN_T>& s_in, hls::stream<ap_uint<64> >& a_job,
                                    hls::stream<ap_uint<64> >& b_job,
                                    hls::stream<ap_uint<64> >& core_job,
                                    hls::stream<ap_uint<64> >& reply,
                                    hls::stream<streamutils::framed_word<WBW> >& pay_a,
                                    hls::stream<streamutils::framed_word<WBW> >& pay_b) {
    // Set by a refused command, cleared only by a reset: then a firing reads nothing.
    static bool halted = false;
#pragma HLS reset variable = halted
    if (halted) return;
    typedef wf_systolic_io_traits<IOID> IO;
    const int LW = IO::a_mem::template lane_capacity<WBW>();  // memory elements per word
    EngineCmd c;
    streamutils::read_msg<WBW>(c, s_in);
    const int op = c.op, m = c.m, kp = c.kp, n = c.n, np = c.npanels, nb = c.nb;
    const bool keep = c.keep_a != 0;
    int st = wf_linalg::OK;
    if (op != 1 && op != 2) {  // waveflow.linalg.systolic.MatmulOp
        st = wf_linalg::BAD_OP;
    } else if (m < 1 || m > MMAX || kp < 1 || kp > KMAX || n < 1 || n > NMAX || np < 1 ||
               np > PMAX || nb < 1 || m % R != 0 || n % C != 0 || (keep && np != 1)) {
        st = wf_linalg::BAD_DIMS;
    }
    EngineReply r;
    r.tx_id = c.tx_id;
    r.status = st;
    r.m = m;
    r.n = n;
    r.nb = (st == wf_linalg::OK) ? nb : 0;
    if (st != wf_linalg::OK) {
        r.template write_stream<64>(reply);
        halted = true;
        return;
    }
    EngineJob j;
    j.op = op;
    j.keep_a = c.keep_a;
    j.m = m;
    j.kp = kp;
    j.n = n;
    j.npanels = np;
    j.nb = nb;
    j.template write_stream<64>(a_job);
    j.template write_stream<64>(b_job);
    j.template write_stream<64>(core_job);
    r.template write_stream<64>(reply);
    const int wa = (m * kp + LW - 1) / LW, wb = (kp * n + LW - 1) / LW;
    const int parts = keep ? 1 + nb : 2 * nb * np;
PARTS:
    for (int x = 0; x < parts; ++x) {
#pragma HLS LOOP_TRIPCOUNT max = 2 * PMAX
        const bool is_a = keep ? (x == 0) : ((x & 1) == 0);
        const int words = is_a ? wa : wb;
    FWD:
        for (int i = 0; i < words; ++i) {
#pragma HLS LOOP_TRIPCOUNT max = (KMAX * NMAX) / 2
#pragma HLS PIPELINE II = 1
            bool last;
            const ap_uint<WBW> d = streamutils::read_boundary_word<IN_T, WBW>(s_in, last);
            if (is_a) {
                streamutils::write_boundary_word<streamutils::framed_word<WBW>, WBW>(pay_a, d, last);
            } else {
                streamutils::write_boundary_word<streamutils::framed_word<WBW>, WBW>(pay_b, d, last);
            }
        }
    }
}

#endif  // WF_SYSTOLIC_ENGINE_RX_TASK_H
