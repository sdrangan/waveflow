// poly_evaluate_impl.tpp
// Included from gen/poly.hpp. Types declared there are in scope.
// Do not include this file directly except via the .hpp.
//
// Hand-written body for the templated `poly::evaluate` hook.  Mechanical
// extraction of the per-iteration body of the while loop in the previous
// hand-written poly.cpp: response-header emit, sample-burst processing,
// and sample-burst framing validation.  The cmd_hdr framing detection
// that lived in the outer loop is intentionally not reproduced here —
// the generated kernel reads cmd_hdr with the one-arg `read_axi4_stream`
// which discards TLAST status.  See plan §"Known semantic regression".

// PolyRespHdr is used inside the hook body but is not visible through the
// generated kernel signature, so gen/poly.hpp doesn't auto-include it.
// Pull it in here — the schema header has its own include guard.
#include "include/poly_resp_hdr.h"

namespace poly_impl {

// Horner-form polynomial evaluator; pulled from the previous hand-written
// poly.cpp lines 4-11.  Kept as a `static inline` helper so multiple TUs
// that include this .tpp don't trip on ODR.
static inline float eval_poly_horner(const float coeff[4], float x) {
#pragma HLS INLINE
    float y = coeff[3];
    y = y * x + coeff[2];
    y = y * x + coeff[1];
    y = y * x + coeff[0];
    return y;
}

template <int in_bw, int out_bw>
ap_uint<8> evaluate(PolyCmdHdr cmd_hdr,
                    hls::stream<streamutils::axi4s_word<in_bw>>& s_in,
                    hls::stream<streamutils::axi4s_word<out_bw>>& m_out,
                    float coeffs[4]) {
    // ----- Emit response header -----
    PolyRespHdr resp_hdr;
    resp_hdr.tx_id = cmd_hdr.tx_id;
    resp_hdr.write_axi4_stream<out_bw>(m_out, true);

    // ----- Process the sample burst lane-by-lane -----
    static const int pf = float32_array_utils::pf<in_bw>();
    float x_lane[pf];
    float y_lane[pf];
#pragma HLS ARRAY_PARTITION variable=x_lane complete dim=1
#pragma HLS ARRAY_PARTITION variable=y_lane complete dim=1

    int nsamp_read = 0;
    streamutils::tlast_status samp_in_tlast = streamutils::tlast_status::no_tlast;
    bool read_done = false;
    for (int i = 0; i < cmd_hdr.nsamp && !read_done; i += pf) {
        const int nrem = cmd_hdr.nsamp - i;
        const int lane_count = (nrem < pf) ? nrem : pf;
        streamutils::tlast_status lane_tlast = streamutils::tlast_status::no_tlast;
        float32_array_utils::read_axi4_stream_lane<in_bw>(
            s_in, x_lane, nrem, lane_tlast);

        for (int k = 0; k < pf; ++k) {
#pragma HLS UNROLL
            if (k < lane_count) {
                y_lane[k] = eval_poly_horner(coeffs, x_lane[k]);
            }
        }

        const bool out_tlast = (nrem <= pf);
        float32_array_utils::write_axi4_stream_lane<out_bw>(
            y_lane, m_out, out_tlast, nrem);

        nsamp_read += lane_count;
        if (lane_tlast == streamutils::tlast_status::tlast_at_end) {
            samp_in_tlast = out_tlast ? streamutils::tlast_status::tlast_at_end
                                      : streamutils::tlast_status::tlast_early;
            read_done = true;
        }
    }

    // ----- Validate framing of the sample burst -----
    if (samp_in_tlast == streamutils::tlast_status::tlast_early) {
        return (ap_uint<8>)static_cast<unsigned int>(PolyError::TLAST_EARLY_SAMP_IN);
    }
    if (samp_in_tlast == streamutils::tlast_status::no_tlast) {
        return (ap_uint<8>)static_cast<unsigned int>(PolyError::NO_TLAST_SAMP_IN);
    }
    if (nsamp_read != cmd_hdr.nsamp) {
        return (ap_uint<8>)static_cast<unsigned int>(PolyError::WRONG_NSAMP);
    }
    return (ap_uint<8>)static_cast<unsigned int>(PolyError::NO_ERROR);
}

} // namespace poly_impl
