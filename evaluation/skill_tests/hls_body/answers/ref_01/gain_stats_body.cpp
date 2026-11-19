// Reference answer, hls_body/01_gain_stats: the kernel body, one firing of the hls::task.
// Compiled and checked bit for bit against gain_model.py by tests/mcp/test_skills.py (plain g++,
// not synthesized).
#include "include/streamutils_hls.h"
#include "include/fixed16_1_array_utils.h"
#include "include/fixed16_2_array_utils.h"
#include "include/gain_cmd.h"
#include "include/gain_resp.h"
#include "include/gain_status.h"

namespace xu = fixed16_1_array_utils;   // the samples, ap_fixed<16, 1>
namespace yu = fixed16_2_array_utils;   // the results, ap_fixed<16, 2, AP_RND, AP_SAT>

// The product rounded as a result is, with room for every product: a result saturated when
// this differs from it (gain_model.Rounded).
typedef ap_fixed<19, 5, AP_RND> rounded_t;

// One firing = one command: the header, a pipelined loop over the sample words, the answers.
template <int W>
void gain_stats(hls::stream<streamutils::axi4s_word<W>>& s_in,
                hls::stream<streamutils::axi4s_word<W>>& m_out,
                hls::stream<streamutils::axi4s_word<W>>& m_status) {
    // The one value kept across commands.  Zero at power-on; reset does not move it, because
    // every firing starts with a blocking read of s_in.
    static ap_uint<32> total;

    GainCmd cmd;
    cmd.read_axi4_stream<W>(s_in);

    GainResp resp;
    resp.tx_id = cmd.tx_id;
    resp.n = cmd.n;
    resp.write_axi4_stream<W>(m_out, false);

    // A word per iteration, the compute unrolled across its lanes: the bus's full rate.
    const int PF = xu::lane_capacity<W>();
    static_assert(PF == yu::lane_capacity<W>(), "samples and results pack alike");
    ap_uint<16> nsat = 0;
SAMPLES:
    for (int i = 0; i < cmd.n; i += PF) {
#pragma HLS PIPELINE II=1
        xu::value_type x[PF];
        yu::value_type y[PF];
#pragma HLS ARRAY_PARTITION variable=x complete dim=1
#pragma HLS ARRAY_PARTITION variable=y complete dim=1
        const int m = (cmd.n - i < PF) ? (int)(cmd.n - i) : PF;   // the tail word may be short
        xu::read_axi4_stream_lane<W>(s_in, x, m);
        ap_uint<8> sat_word = 0;
        for (int k = 0; k < PF; ++k) {
#pragma HLS UNROLL
            const ap_fixed<32, 4> p = cmd.gain * x[k];             // exact
            y[k] = p;                                               // rounded, saturated
            const rounded_t r = p;
            if (k < m && r != y[k]) ++sat_word;
        }
        nsat += sat_word;
        yu::write_axi4_stream_lane<W>(y, m_out, i + PF >= cmd.n, m);   // TLAST on the last word
    }

    total += cmd.n;
    GainStatus st;
    st.tx_id = cmd.tx_id;
    st.nsat = nsat;
    st.total = total;
    st.write_axi4_stream<W>(m_status, true);
}
