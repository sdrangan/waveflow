// cg_vector_rx_task.h -- the standalone CG vector unit's receiver.
//
// A job is a START request (k, n, nfollow = nit; payload B, k x n) and then nit STEP requests
// (the same k and n, nfollow = the steps still to come; payload S, k x n); each payload is one
// burst of memory elements (wf_matrix_io.h).  Every request is validated: the operation (BAD_OP);
// its place in the job (BAD_SEQUENCE: a STEP outside a job, a START inside one, a STEP whose k, n
// or nfollow disagree with its job); a START's dimensions and iteration count against the core's
// maxima (BAD_DIMS: waveflow.linalg.cg_vector.cmd_status); the length against the dimensions
// (BAD_LENGTH).  An accepted START sends the job (CgVectorCmd) to the loader and the core.  Every
// request sends the store its reply header (the request's header with the status and the reply's
// length; nfollow is kept, so the store knows the job's last reply).  An accepted request's payload
// goes to the loader; a rejected one is drained by its length and leaves the job as it was.
//
// One firing is one request outside a job, or one whole job with every request rejected inside it,
// so the job's state lives in the firing.  Python twin: waveflow.linalg.cg_vector.CgVectorRx.
#ifndef WF_CG_VECTOR_RX_TASK_H
#define WF_CG_VECTOR_RX_TASK_H
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "wf_cg_vector_cmd.h"
#include "wf_linalg_msg.h"
#include "wf_linalg_traits.h"
#include "cg_vector_task.h"

template <int WBW, int KMAX, int NMAX, int NITMAX, int L, int IOID>
static void cg_vector_rx_task(hls::stream<streamutils::framed_word<WBW> >& s_in,
                              hls::stream<ap_uint<64> >& load_cmd,
                              hls::stream<ap_uint<64> >& core_cmd,
                              hls::stream<streamutils::framed_word<WBW> >& store_cmd,
                              hls::stream<streamutils::framed_word<WBW> >& s_pay) {
    typedef wf_cg_io_traits<IOID> IO;
    const int LW = IO::b_mem::template lane_capacity<WBW>();  // memory elements per word
    bool in_job = false;
    int jk = 0, jn = 0, left = 0;  // the job's dimensions, and the steps still to come
REQUESTS:
    do {
#pragma HLS LOOP_TRIPCOUNT max = NITMAX + 1
        LinalgHeader h;
        streamutils::tlast_status tl;
        h.template read_framed_stream<WBW>(s_in, tl);
        const int op = h.op, k = h.k, n = h.n, nf = h.nfollow;
        const ap_uint<32> words = (ap_uint<32>(k) * ap_uint<32>(n) + LW - 1) / LW;
        int st = wf_linalg::OK;
        if (op != wf_cg::START && op != wf_cg::STEP) {
            st = wf_linalg::BAD_OP;
        } else if (op == wf_cg::START) {
            if (in_job) {
                st = wf_linalg::BAD_SEQUENCE;
            } else if (nf < 1 || nf > NITMAX || k < 1 || k > KMAX || n < 1 || n > NMAX ||
                       (n & (L - 1)) != 0) {
                st = wf_linalg::BAD_DIMS;
            }
        } else if (!in_job || k != jk || n != jn || nf != left - 1) {
            st = wf_linalg::BAD_SEQUENCE;
        }
        if (st == wf_linalg::OK && h.length != words) st = wf_linalg::BAD_LENGTH;
        LinalgHeader r = h;
        r.status = st;
        r.length = (st == wf_linalg::OK) ? words : ap_uint<32>(0);
        r.template write_framed_stream<WBW>(store_cmd);
        if (st == wf_linalg::OK) {
            if (op == wf_cg::START) {
                CgVectorCmd c;
                c.nit = nf;
                c.k = k;
                c.n = n;
                c.template write_stream<64>(load_cmd);
                c.template write_stream<64>(core_cmd);
                in_job = true;
                jk = k;
                jn = n;
                left = nf;
            } else {
                left -= 1;
                in_job = left != 0;
            }
        FWD:
            for (ap_uint<32> i = 0; i < h.length; ++i) {
#pragma HLS LOOP_TRIPCOUNT max = (KMAX * NMAX) / 2
#pragma HLS PIPELINE II = 1
                s_pay.write(s_in.read());
            }
        } else {
            wf_linalg::drain<WBW>(s_in, h.length);
        }
    } while (in_job);
}

#endif  // WF_CG_VECTOR_RX_TASK_H
