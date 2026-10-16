// Hand-written error-path testbench: a DATA transaction whose sample burst ends
// 2 words early (TLAST on word nsamp-3), then END.  The kernel must halt with
// TLAST_EARLY_SAMP_IN and report it through its register-field references.
#include "polyb.hpp"
#include "include/streamutils_tb.h"
#include "include/float32_array_utils_tb.h"
#include "include/poly_cmd_hdr.h"
#include <cstdio>
#include <string>

int main(int argc, char** argv) {
    const std::string data_dir = (argc > 1) ? argv[1] : "data";
    hls::stream<streamutils::axi4s_word<32>> s_in, m_out;
    ap_uint<1> halted = 0;
    ap_uint<8> error = 0;
    ap_uint<16> tx_id = 0;
    float coeffs[4] = {};
    float32_array_utils::read_uint32_file_array(coeffs, (data_dir + "/coeffs.bin").c_str(), 4);

    PolyCmdHdr hdr;
    streamutils::read_uint32_file(hdr, (data_dir + "/data_cmd_hdr.bin").c_str());
    float samp[128] = {};
    float32_array_utils::read_uint32_file_array(samp, (data_dir + "/samp_in_data.bin").c_str(), hdr.nsamp);
    PolyCmdHdr end_hdr;
    streamutils::read_uint32_file(end_hdr, (data_dir + "/end_cmd_hdr.bin").c_str());

    hdr.write_axi4_stream<32>(s_in, true);
    float32_array_utils::write_axi4_stream<32>(s_in, samp, true, hdr.nsamp - 2);   // early TLAST
    end_hdr.write_axi4_stream<32>(s_in, true);

    polyb(s_in, m_out, halted, error, tx_id, coeffs);

    int words = 0;
    while (!m_out.empty()) { m_out.read(); ++words; }   // drain only what was produced
    std::printf("GATE_STATUS halted=%d error=%d tx_id=%d out_words=%d expected_tx_id=%d\n",
                (int)halted, (int)error, (int)tx_id, words, (int)hdr.tx_id);
    const bool ok = halted == 1 && error == 3 && tx_id == hdr.tx_id;
    std::printf(ok ? "GATE_ERRPATH_PASS\n" : "GATE_ERRPATH_FAIL\n");
    return ok ? 0 : 1;
}
