#ifndef EXAMPLES_STREAM_KERNEL_POLY_KERNEL_TASK_H
#define EXAMPLES_STREAM_KERNEL_POLY_KERNEL_TASK_H
// poly_kernel_task.h -- the free-running polynomial kernel (HAND-WRITTEN), the HLS twin of
// examples/stream_kernel/stream_kernel.py::PolyKernel.run_iter.
//
// ONE FIRING = ONE COMMAND, written as the Python reads (the command-response pattern,
// docs/guide/patterns/command_response.md):
//
//   1. read the CmdHdr off s_in (its coefficients ride in it: nothing carries over);
//   2. the samples: a pipelined loop of ONE WORD PER ITERATION -- read a word, evaluate its lanes,
//      write the result word -- with the generated float32 lane routines
//      (docs/guide/vectorization/hls/loop_optimization.md).  The result that completes nsamp carries
//      TLAST; so does the one answering an input word whose TLAST came early, which ends the loop;
//   3. the response: tx_id, the samples processed, and the status -- OK, or TLAST_EARLY.
//
// No halt and no status register: an error is answered on m_resp and the next firing takes the next
// command.
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "float32_array_utils.h"
#include "cmd_hdr.h"
#include "resp.h"

namespace poly_kernel_body {

// y = c0 + c1 x + c2 x^2 + c3 x^3 in Horner order.  Every multiply and add is its own statement, so
// the compiler cannot fuse y*x + c into one multiply-add (which rounds once instead of twice and would
// no longer match poly_eval()).
static inline float horner(const float c[4], float x) {
#pragma HLS INLINE
    float y = c[3];
    y = y * x;
    y = y + c[2];
    y = y * x;
    y = y + c[1];
    y = y * x;
    y = y + c[0];
    return y;
}

}  // namespace poly_kernel_body

template <int W>
static void poly_kernel_task(hls::stream<streamutils::axi4s_word<W> >& s_in,
                             hls::stream<streamutils::axi4s_word<W> >& m_out,
                             hls::stream<streamutils::axi4s_word<W> >& m_resp) {
    // 1. the command
    CmdHdr hdr;
    hdr.read_axi4_stream<W>(s_in);
    float c[4];
#pragma HLS ARRAY_PARTITION variable=c complete dim=1
    for (int k = 0; k < 4; ++k) {
#pragma HLS UNROLL
        c[k] = hdr.coeffs.data[k];
    }

    // 2. the samples, a word per iteration
    static const int pf = float32_array_utils::pf<W>();
    float x_lane[pf];
    float y_lane[pf];
#pragma HLS ARRAY_PARTITION variable=x_lane complete dim=1
#pragma HLS ARRAY_PARTITION variable=y_lane complete dim=1
    ap_uint<16> done = 0;
    bool early = false;
SAMPLES:
    for (int i = 0; i < hdr.nsamp; i += pf) {
#pragma HLS PIPELINE II=1
        const int nrem = hdr.nsamp - i;
        const int lanes = (nrem < pf) ? nrem : pf;
        const bool final_word = (nrem <= pf);
        streamutils::tlast_status ts = streamutils::tlast_status::no_tlast;
        float32_array_utils::read_axi4_stream_lane<W>(s_in, x_lane, nrem, ts);
        const bool in_tlast = (ts == streamutils::tlast_status::tlast_at_end);
        for (int k = 0; k < pf; ++k) {
#pragma HLS UNROLL
            if (k < lanes) {
                y_lane[k] = poly_kernel_body::horner(c, x_lane[k]);
            }
        }
        float32_array_utils::write_axi4_stream_lane<W>(y_lane, m_out, final_word || in_tlast, nrem);
        done = i + lanes;
        if (in_tlast && !final_word) {
            early = true;
            break;
        }
    }

    // 3. the response
    Resp resp;
    resp.tx_id = hdr.tx_id;
    resp.nsamp = done;
    resp.status = early ? Status::TLAST_EARLY : Status::OK;
    resp.write_axi4_stream<W>(m_resp, true);
}

#endif  // EXAMPLES_STREAM_KERNEL_POLY_KERNEL_TASK_H
