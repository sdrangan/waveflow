// poly_body_impl.tpp -- the WHOLE kernel body, hand-written.
//
// Included from gen/poly.hpp.  The generated top (gen/poly.cpp) holds every interface
// pragma and calls body() with every kernel argument; the register fields are references,
// so this code writes halted / error / tx_id itself.  The Python twin, bit for bit, is
// poly_stream_model() in poly.py.

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

// One DATA transaction: the response header, then the sample burst.  Returns the error code.
template <int in_bw, int out_bw>
ap_uint<8> transaction(PolyCmdHdr cmd_hdr,
                       hls::stream<streamutils::axi4s_word<in_bw>>& s_in,
                       hls::stream<streamutils::axi4s_word<out_bw>>& m_out,
                       float coeffs[4]) {
#pragma HLS INLINE
    PolyRespHdr resp_hdr;
    resp_hdr.tx_id = cmd_hdr.tx_id;
    resp_hdr.write_axi4_stream<out_bw>(m_out, true);

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
        float32_array_utils::read_axi4_stream_lane<in_bw>(s_in, x_lane, nrem, lane_tlast);
        for (int k = 0; k < pf; ++k) {
#pragma HLS UNROLL
            if (k < lane_count) {
                y_lane[k] = eval_poly_horner(coeffs, x_lane[k]);
            }
        }
        const bool out_tlast = (nrem <= pf);
        float32_array_utils::write_axi4_stream_lane<out_bw>(y_lane, m_out, out_tlast, nrem);
        nsamp_read += lane_count;
        if (lane_tlast == streamutils::tlast_status::tlast_at_end) {
            samp_in_tlast = out_tlast ? streamutils::tlast_status::tlast_at_end
                                      : streamutils::tlast_status::tlast_early;
            read_done = true;
        }
    }
    if (cmd_hdr.nsamp != 0) {
        if (samp_in_tlast == streamutils::tlast_status::tlast_early)
            return (ap_uint<8>)static_cast<unsigned int>(PolyError::TLAST_EARLY_SAMP_IN);
        if (samp_in_tlast == streamutils::tlast_status::no_tlast)
            return (ap_uint<8>)static_cast<unsigned int>(PolyError::NO_TLAST_SAMP_IN);
    }
    if (nsamp_read != cmd_hdr.nsamp)
        return (ap_uint<8>)static_cast<unsigned int>(PolyError::WRONG_NSAMP);
    return (ap_uint<8>)static_cast<unsigned int>(PolyError::NO_ERROR);
}

// The kernel: a persistent loop over in-band commands, until END or an error.
template <int in_bw, int out_bw>
void body(hls::stream<streamutils::axi4s_word<in_bw>>& s_in,
          hls::stream<streamutils::axi4s_word<out_bw>>& m_out,
          ap_uint<1>& halted, ap_uint<8>& error, ap_uint<16>& tx_id, float coeffs[4]) {
#pragma HLS INLINE
    while (true) {
        PolyCmdHdr cmd_hdr;
        cmd_hdr.read_axi4_stream<in_bw>(s_in);
        if (cmd_hdr.cmd_type == PolyCmdType::END) {
            return;
        }
        ap_uint<8> err = transaction<in_bw, out_bw>(cmd_hdr, s_in, m_out, coeffs);
        if (err != (ap_uint<8>)static_cast<unsigned int>(PolyError::NO_ERROR)) {
            error = err;
            tx_id = cmd_hdr.tx_id;
            halted = 1;
            return;
        }
    }
}

}  // namespace poly_impl
