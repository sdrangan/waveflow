// Reference answer, 01_pulse_report: the kernel body (written, not compiled or synthesized).
// Throughput is not scored by this test: a word with several hits writes several PEAKs.
#include "include/streamutils_hls.h"
#include "include/fixed12_1_array_utils.h"
#include "include/pulse_cmd.h"
#include "include/start.h"
#include "include/peak.h"
#include "include/done.h"

namespace au = fixed12_1_array_utils;

template <int W>
void pulse_det(hls::stream<streamutils::axi4s_word<W>>& s_in,
               hls::stream<streamutils::axi4s_word<W>>& m_out) {
    static const int LW = au::lane_capacity<W>();
    while (true) {
        PulseCmd cmd;
        cmd.read_axi4_stream<W>(s_in);                 // the whole command, by its schema
        if (cmd.op == Op::END) return;

        Start start;
        start.kind = Kind::START;
        start.tx_id = cmd.tx_id;
        start.write_axi4_stream<W>(m_out, false);

        ap_uint<16> count = 0;
        au::value_type x[LW];
#pragma HLS ARRAY_PARTITION variable=x complete dim=1
        for (int i = 0; i < cmd.nsamp; i += LW) {      // one sample word per iteration
            const int n = (cmd.nsamp - i < LW) ? (cmd.nsamp - i) : LW;
            streamutils::tlast_status tl;
            au::read_axi4_stream_lane<W>(s_in, x, n, tl);
            for (int k = 0; k < LW; ++k) {
#pragma HLS UNROLL
                if (k < n && x[k] > cmd.thresh) {      // compared as ap_fixed values
                    Peak p;
                    p.kind = Kind::PEAK;
                    p.index = i + k;
                    p.value = x[k];
                    p.write_axi4_stream<W>(m_out, false);
                    ++count;
                }
            }
        }

        Done done;
        done.kind = Kind::DONE;
        done.tx_id = cmd.tx_id;
        done.count = count;
        done.write_axi4_stream<W>(m_out, true);        // TLAST on DONE only
    }
}
