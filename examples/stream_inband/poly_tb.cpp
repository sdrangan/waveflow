// poly_tb.cpp -- the hand-written C++ testbench.
//
// Usage: poly_tb <data_dir> <stage> [scenario]
//
// For every scenario named in <data_dir>/scenarios.txt (or just [scenario]), it plays the
// stimulus <data_dir>/<scenario>/in into the kernel, runs it, and records the response
// into <data_dir>/<scenario>/<stage>/ with the final register status in status.json.
// <stage> is "csim" or "cosim", so both runs keep their own results.  The output
// directories must exist; the Python build step that runs this creates them.
//
// The stimulus is the same file the Python model reads (poly.py, poly_stream_model):
// one input, two implementations, and scenarios.py checks both against the expected
// outputs it computed from each scenario's intent.

#include "gen/poly.hpp"
#include "include/bundle_tb.h"
#include "include/float32_array_utils_tb.h"

#include <cstdio>
#include <fstream>
#include <string>

int main(int argc, char** argv) {
    if (argc < 3) {
        std::fprintf(stderr, "usage: poly_tb <data_dir> <stage> [scenario]\n");
        return 2;
    }
    const std::string root = argv[1];
    const std::string stage = argv[2];
    const std::string only = (argc > 3) ? argv[3] : "";

    std::ifstream list(root + "/scenarios.txt");
    std::string name;
    int run = 0;
    while (list >> name) {
        if (!only.empty() && name != only) continue;
        const std::string dir = root + "/" + name;

        hls::stream<streamutils::axi4s_word<32>> s_in, m_out;
        ap_uint<1> halted = 0;
        ap_uint<8> error = 0;
        ap_uint<16> tx_id = 0;
        float coeffs[4] = {};
        float32_array_utils::read_uint32_file_array(coeffs, (dir + "/coeffs.bin").c_str(), 4);

        wf::play_stream<32>(dir + "/in", s_in);
        poly(s_in, m_out, halted, error, tx_id, coeffs);
        wf::record_stream<32>(m_out, dir + "/" + stage);
        while (!s_in.empty()) s_in.read();   // a halted kernel leaves its input unread

        std::ofstream st(dir + "/" + stage + "/status.json");
        st << "{\"halted\": " << (int)halted << ", \"error\": " << (int)error
           << ", \"tx_id\": " << (int)tx_id << "}\n";
        std::printf("scenario %-14s halted=%d error=%d tx_id=%d\n", name.c_str(),
                    (int)halted, (int)error, (int)tx_id);
        ++run;
    }
    if (run == 0) {
        std::fprintf(stderr, "no scenario ran (data_dir=%s, only=%s)\n", root.c_str(), only.c_str());
        return 1;
    }
    return 0;
}
