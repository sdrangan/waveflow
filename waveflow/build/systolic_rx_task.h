// systolic_rx_task.h -- the standalone systolic unit's receiver: one request message per firing.
//
// A request is a LinalgHeader burst (wf_linalg_msg.h) and its payload: A as the job states it
// (m x k, or k x m for A^H), then B (k x n), each one burst of memory elements (wf_matrix_io.h).
// The header is validated: the operation (BAD_OP), nfollow == 0 (BAD_SEQUENCE: jobs are
// self-contained), the dimensions against the core's maxima and array (BAD_DIMS:
// waveflow.linalg.systolic.cmd_status), and length against the dimensions (BAD_LENGTH).  A valid
// request sends the job to the load task and the core, the reply header to the store, and its
// payload words to the load task.  A rejected one sends the store a reply with its status and no
// payload, and is drained by length.  Python twin: waveflow.linalg.systolic.SystolicRx.
#ifndef WF_SYSTOLIC_RX_TASK_H
#define WF_SYSTOLIC_RX_TASK_H
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "wf_linalg_msg.h"
#include "wf_linalg_traits.h"
#include "wf_systolic_cmd.h"

template <int WBW, int MMAX, int KMAX, int NMAX, int L, int R, int C, int IOID>
static void systolic_rx_task(hls::stream<streamutils::framed_word<WBW> >& s_in,
                             hls::stream<ap_uint<64> >& load_cmd,
                             hls::stream<ap_uint<64> >& core_cmd,
                             hls::stream<streamutils::framed_word<WBW> >& store_cmd,
                             hls::stream<streamutils::framed_word<WBW> >& s_pay) {
    typedef wf_systolic_io_traits<IOID> IO;
    const int LW = IO::a_mem::template lane_capacity<WBW>();  // memory elements per word
    LinalgHeader h;
    streamutils::tlast_status tl;
    h.template read_framed_stream<WBW>(s_in, tl);
    const int op = h.op, m = h.m, k = h.k, n = h.n;
    // k is a multiple of L, or a power of two below it (L is one)
    const bool k_ok = (k & (L - 1)) == 0 || (k < L && (k & (k - 1)) == 0);
    const ap_uint<32> wa = (m * k + LW - 1) / LW, wb = (k * n + LW - 1) / LW;
    int st = wf_linalg::OK;
    if (op != 1 && op != 2) {  // waveflow.linalg.systolic.MatmulOp
        st = wf_linalg::BAD_OP;
    } else if (h.nfollow != 0) {
        st = wf_linalg::BAD_SEQUENCE;
    } else if (m < 1 || m > MMAX || k < 1 || k > KMAX || n < 1 || n > NMAX || m % R != 0 ||
               n % C != 0 || !k_ok) {
        st = wf_linalg::BAD_DIMS;
    } else if (h.length != wa + wb) {
        st = wf_linalg::BAD_LENGTH;
    }
    if (st == wf_linalg::OK) {
        SystolicCmd c;
        c.op = op;
        c.nb = 1;
        c.m = m;
        c.k = k;
        c.n = n;
        c.template write_stream<64>(load_cmd);
        c.template write_stream<64>(core_cmd);
        wf_linalg::reply<WBW>(store_cmd, h, wf_linalg::OK, (m * n + LW - 1) / LW);
    FWD:
        for (ap_uint<32> i = 0; i < h.length; ++i) {
#pragma HLS LOOP_TRIPCOUNT max = (MMAX * KMAX + KMAX * NMAX) / 2
#pragma HLS PIPELINE II = 1
            s_pay.write(s_in.read());
        }
    } else {
        wf_linalg::reply<WBW>(store_cmd, h, st, 0);
        wf_linalg::drain<WBW>(s_in, h.length);
    }
}

#endif  // WF_SYSTOLIC_RX_TASK_H
