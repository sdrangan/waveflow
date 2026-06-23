// fir_tb.cpp — self-contained testbench for the FIR sandbox.
//
// Loads the little-endian float32 fixtures written by gen_data.py from a data
// directory (passed as argv[1], the `csim_design -argv "$data_dir"` convention),
// runs fir_accel, and compares the result against Y_golden.bin *bit-exactly*
// (the golden is accumulated in the same tap order as fir_compute, so no
// floating-point tolerance is needed).  Returns non-zero on mismatch so the
// run.tcl driver reports WAVEFLOW_ERROR.
#include "fir_sandbox.hpp"

#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>

using fir_sandbox::real_t;
using fir_sandbox::T;

namespace {

// Minimal extractor for a flat "key": <int> field in meta.json (controlled
// format from gen_data.py — no general JSON parser needed).
int meta_int(const std::string& text, const std::string& key) {
    const std::string needle = "\"" + key + "\"";
    size_t p = text.find(needle);
    if (p == std::string::npos) {
        std::fprintf(stderr, "meta.json: key '%s' not found\n", key.c_str());
        std::exit(2);
    }
    p = text.find(':', p);
    return std::atoi(text.c_str() + p + 1);
}

std::vector<float> read_f32(const std::string& path, size_t count) {
    std::ifstream f(path, std::ios::binary);
    if (!f) {
        std::fprintf(stderr, "cannot open %s\n", path.c_str());
        std::exit(2);
    }
    std::vector<float> v(count);
    f.read(reinterpret_cast<char*>(v.data()),
           static_cast<std::streamsize>(count * sizeof(float)));
    if (static_cast<size_t>(f.gcount()) != count * sizeof(float)) {
        std::fprintf(stderr, "short read on %s\n", path.c_str());
        std::exit(2);
    }
    return v;
}

std::string read_text(const std::string& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) {
        std::fprintf(stderr, "cannot open %s\n", path.c_str());
        std::exit(2);
    }
    return std::string((std::istreambuf_iterator<char>(f)),
                       std::istreambuf_iterator<char>());
}

}  // namespace

int main(int argc, char** argv) {
    const std::string data_dir = (argc > 1) ? argv[1] : "data";

    const std::string meta = read_text(data_dir + "/meta.json");
    const int n_rows = meta_int(meta, "n_rows");
    const int n_cols = meta_int(meta, "n_cols");
    const int t_meta = meta_int(meta, "T");
    if (t_meta != T) {
        std::fprintf(stderr, "meta T=%d != kernel T=%d\n", t_meta, T);
        return 2;
    }
    const int out_len = n_cols - T + 1;

    std::vector<float> X = read_f32(data_dir + "/X.bin",
                                    static_cast<size_t>(n_rows) * n_cols);
    std::vector<float> h = read_f32(data_dir + "/h.bin", T);
    std::vector<float> Yg = read_f32(data_dir + "/Y_golden.bin",
                                     static_cast<size_t>(n_rows) * out_len);
    std::vector<float> Y(static_cast<size_t>(n_rows) * out_len, 0.0f);

    fir_accel(X.data(), Y.data(), h.data(), n_rows, n_cols);

    // Dump the kernel output for offline inspection (RTL output during cosim
    // post-check; C-model output during csim / cosim recording).
    {
        std::ofstream of(data_dir + "/Y_out.bin", std::ios::binary);
        of.write(reinterpret_cast<const char*>(Y.data()),
                 static_cast<std::streamsize>(Y.size() * sizeof(float)));
    }

    // Bit-exact comparison (raw 32-bit pattern), so e.g. a sign-of-zero or
    // a single-ULP reorder would fail loudly.
    int mismatches = 0;
    for (size_t i = 0; i < Y.size(); ++i) {
        uint32_t a, b;
        std::memcpy(&a, &Y[i], 4);
        std::memcpy(&b, &Yg[i], 4);
        if (a != b) {
            if (mismatches < 8) {
                std::fprintf(stderr,
                             "mismatch at %zu: got %.9g (0x%08x) exp %.9g (0x%08x)\n",
                             i, Y[i], a, Yg[i], b);
            }
            ++mismatches;
        }
    }

    if (mismatches == 0) {
        std::printf("WAVEFLOW_FIR_CSIM_OK: Y == Y_golden bit-exact "
                    "(n_rows=%d n_cols=%d T=%d out_len=%d, %zu elements)\n",
                    n_rows, n_cols, T, out_len, Y.size());
        return 0;
    }
    std::fprintf(stderr, "WAVEFLOW_FIR_CSIM_FAIL: %d / %zu elements mismatch\n",
                 mismatches, Y.size());
    return 1;
}
