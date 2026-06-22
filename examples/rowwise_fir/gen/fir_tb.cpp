// Generated-top FIR cosim/csim TB: load the Phase-1 fixtures into one mem image
// (X | h | Y), run the generated fir() top, and check the Y region bit-exact vs
// Y_golden.bin (produced by the ONE shared fir_golden).
#include "fir.hpp"
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <fstream>
#include <string>
#include <vector>

void fir(real_t* gmem, int x_off, int y_off, int h_off, int n_rows, int n_cols);

static int meta_int(const std::string& s, const std::string& key) {
    const std::string n = "\"" + key + "\"";
    size_t p = s.find(n);
    if (p == std::string::npos) { std::fprintf(stderr, "meta key %s missing\n", key.c_str()); std::exit(2); }
    p = s.find(':', p);
    return std::atoi(s.c_str() + p + 1);
}
static std::vector<float> rdf(const std::string& p, size_t n) {
    std::ifstream f(p, std::ios::binary);
    if (!f) { std::fprintf(stderr, "open %s\n", p.c_str()); std::exit(2); }
    std::vector<float> v(n);
    f.read(reinterpret_cast<char*>(v.data()), (std::streamsize)(n * sizeof(float)));
    return v;
}
static std::string rdt(const std::string& p) {
    std::ifstream f(p, std::ios::binary);
    if (!f) { std::fprintf(stderr, "open %s\n", p.c_str()); std::exit(2); }
    return std::string((std::istreambuf_iterator<char>(f)), std::istreambuf_iterator<char>());
}

int main(int argc, char** argv) {
    const std::string d = (argc > 1) ? argv[1] : "data";
    const std::string meta = rdt(d + "/meta.json");
    const int n_rows = meta_int(meta, "n_rows");
    const int n_cols = meta_int(meta, "n_cols");
    const int t = fir_dataflow::T;
    const int out_len = n_cols - t + 1;
    const int x_off = 0;
    const int h_off = n_rows * n_cols;
    const int y_off = h_off + t;
    const int depth = y_off + n_rows * out_len;

    std::vector<float> X = rdf(d + "/X.bin", (size_t)n_rows * n_cols);
    std::vector<float> h = rdf(d + "/h.bin", (size_t)t);
    std::vector<float> Yg = rdf(d + "/Y_golden.bin", (size_t)n_rows * out_len);
    std::vector<float> mem((size_t)depth, 0.0f);
    std::memcpy(&mem[x_off], X.data(), X.size() * sizeof(float));
    std::memcpy(&mem[h_off], h.data(), h.size() * sizeof(float));

    fir(mem.data(), x_off, y_off, h_off, n_rows, n_cols);

    int bad = 0;
    for (size_t i = 0; i < Yg.size(); ++i) {
        uint32_t a, b;
        std::memcpy(&a, &mem[y_off + i], 4);
        std::memcpy(&b, &Yg[i], 4);
        if (a != b) {
            if (bad < 8) std::fprintf(stderr, "mismatch %zu: got %.9g exp %.9g\n", i, mem[y_off+i], Yg[i]);
            ++bad;
        }
    }
    if (bad == 0) {
        std::printf("WAVEFLOW_FIR_GEN_OK: Y == Y_golden bit-exact (n_rows=%d n_cols=%d out_len=%d)\n",
                    n_rows, n_cols, out_len);
        return 0;
    }
    std::fprintf(stderr, "WAVEFLOW_FIR_GEN_FAIL: %d/%zu mismatch\n", bad, Yg.size());
    return 1;
}
