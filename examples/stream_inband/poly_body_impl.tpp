// poly_body_impl.tpp -- the WHOLE kernel body, hand-written.
//
// Included from gen/poly.hpp.  The generated tops (gen/poly.cpp: poly at 32 bits,
// poly_bw64 at 64) hold every interface pragma and call body() with every kernel argument;
// the status registers are references, so this code writes halted / error / tx_id itself.
// The Python twin, bit for bit, is poly_stream_model() in poly.py.
//
// The contract (docs/examples/stream_inband/index.md) in the code:
//   rule 2  everything the kernel computes with arrives on s_in: each DATA header carries
//           its own coefficients, and nothing is kept between commands (rule 4);
//   rule 5  the status is cleared at the start of every activation;
//   rule 6  on an error: set the status, close the output burst with TLAST, and return
//           without reading anything more from s_in.

#include "include/float32_array_utils.h"
#include "include/poly_cmd_hdr.h"
#include "include/poly_resp_hdr.h"

namespace poly_impl {

// y = c0 + c1 x + c2 x^2 + c3 x^3 in Horner order.  Every multiply and add is its own
// statement: written as one expression, the compiler may fuse y*x + c into a single
// multiply-add, which rounds once instead of twice and no longer matches poly_eval().
static inline float eval_poly_horner(const float coeff[4], float x) {
#pragma HLS INLINE
    float y = coeff[3];
    y = y * x;
    y = y + coeff[2];
    y = y * x;
    y = y + coeff[1];
    y = y * x;
    y = y + coeff[0];
    return y;
}

static inline ap_uint<8> code(PolyError e) {
#pragma HLS INLINE
    return (ap_uint<8>)static_cast<unsigned int>(e);
}

// One DATA command: the response header, then the results.  Returns the error code.
template <int in_bw, int out_bw>
ap_uint<8> transaction(const PolyCmdHdr& cmd_hdr,
                       hls::stream<streamutils::axi4s_word<in_bw>>& s_in,
                       hls::stream<streamutils::axi4s_word<out_bw>>& m_out) {
#pragma HLS INLINE
    PolyRespHdr resp_hdr;
    resp_hdr.tx_id = cmd_hdr.tx_id;
    resp_hdr.write_axi4_stream<out_bw>(m_out, true);

    float coeffs[4];
#pragma HLS ARRAY_PARTITION variable=coeffs complete dim=1
    for (int k = 0; k < 4; ++k) {
#pragma HLS UNROLL
        coeffs[k] = cmd_hdr.coeffs.data[k];
    }

    static const int pf = float32_array_utils::pf<in_bw>();
    float x_lane[pf];
    float y_lane[pf];
#pragma HLS ARRAY_PARTITION variable=x_lane complete dim=1
#pragma HLS ARRAY_PARTITION variable=y_lane complete dim=1

    // A word per iteration: read it, compute its lanes, write the result word.  The output
    // word gets TLAST when it completes nsamp -- or when the input word had TLAST early,
    // so a burst that ends early is still closed (rule 6).
    ap_uint<8> err = code(PolyError::NO_ERROR);
    for (int i = 0; i < cmd_hdr.nsamp; i += pf) {
        const int nrem = cmd_hdr.nsamp - i;
        const int lane_count = (nrem < pf) ? nrem : pf;
        const bool final_word = (nrem <= pf);
        streamutils::tlast_status lane_tlast = streamutils::tlast_status::no_tlast;
        float32_array_utils::read_axi4_stream_lane<in_bw>(s_in, x_lane, nrem, lane_tlast);
        const bool in_tlast = (lane_tlast == streamutils::tlast_status::tlast_at_end);
        for (int k = 0; k < pf; ++k) {
#pragma HLS UNROLL
            if (k < lane_count) {
                y_lane[k] = eval_poly_horner(coeffs, x_lane[k]);
            }
        }
        float32_array_utils::write_axi4_stream_lane<out_bw>(y_lane, m_out,
                                                            final_word || in_tlast, nrem);
        if (in_tlast && !final_word) {
            err = code(PolyError::TLAST_EARLY_SAMP_IN);
            break;
        }
        if (final_word && !in_tlast) {
            err = code(PolyError::NO_TLAST_SAMP_IN);
        }
    }
    return err;
}

// The kernel: one activation, a loop over in-band commands until END or an error.
template <int in_bw, int out_bw>
void body(hls::stream<streamutils::axi4s_word<in_bw>>& s_in,
          hls::stream<streamutils::axi4s_word<out_bw>>& m_out,
          ap_uint<1>& halted, ap_uint<8>& error, ap_uint<16>& tx_id) {
#pragma HLS INLINE
    halted = 0;                                   // rule 5
    error = code(PolyError::NO_ERROR);
    tx_id = 0;
    while (true) {
        PolyCmdHdr cmd_hdr;
        cmd_hdr.read_axi4_stream<in_bw>(s_in);
        if (cmd_hdr.cmd_type == PolyCmdType::END) {
            return;
        }
        ap_uint<8> err = transaction<in_bw, out_bw>(cmd_hdr, s_in, m_out);
        if (err != code(PolyError::NO_ERROR)) {   // rule 6: the burst is closed; stop here
            error = err;
            tx_id = cmd_hdr.tx_id;
            halted = 1;
            return;
        }
    }
}

}  // namespace poly_impl
