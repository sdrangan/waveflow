// gemv_intlike_tb.cpp -- testbench for every DUT whose data is INTEGER-VALUED on the wire:
// int32, ap_uint<32>, and the two ap_fixed widths.
//
// One source, compiled once per DUT with -DWF_DUT=<n>.  All four take the dot_dsp path (one
// accumulator, index order, no tree), and all four are fed and read as STORED INTEGERS -- the
// .range() field for ap_fixed, the value itself for the integer types.  A decimal round-trip
// would insert a rounding step between the test data and the arithmetic under test.
//
//   argv[1] = input file, argv[2] = output file
#include "gemv_top.hpp"
#include "gemv_tb_common.hpp"

#ifndef WF_DUT
#error "compile with -DWF_DUT=0..3"
#endif

#if WF_DUT == 0
  #define WF_T T_i32
  #define WF_TOP gemv_i32_top
  #define WF_M GEMVI_M
  #define WF_N GEMVI_N
  #define WF_W 32
  #define WF_TAG "int32_t (signed)"
  #define WF_SET(dst, src) dst = (T_i32)(src)
  #define WF_GET(v) ((long long)(v))
  #define WF_AS_VALUE 1
#elif WF_DUT == 1
  #define WF_T T_u32
  #define WF_TOP gemv_u32_top
  #define WF_M GEMVI_M
  #define WF_N GEMVI_N
  #define WF_W 32
  #define WF_TAG "ap_uint<32> (unsigned)"
  #define WF_SET(dst, src) dst = (T_u32)(unsigned int)(src)
  #define WF_GET(v) ((long long)(v).to_uint64())
  #define WF_AS_VALUE 1
#elif WF_DUT == 2
  #define WF_T T_fixed
  #define WF_TOP gemv_fixed_top
  #define WF_M GEMVF_M
  #define WF_N GEMVF_N
  #define WF_W GEMVF_W
  #define WF_TAG "ap_fixed<16,8>"
  #define WF_SET(dst, src) dst.range() = (ap_uint<GEMVF_W>)(src)
  #define WF_GET(v) ((long long)(v).range().to_uint64())
#elif WF_DUT == 3
  #define WF_T T_fix24
  #define WF_TOP gemv_fix24_top
  #define WF_M GEMVF24_M
  #define WF_N GEMVF24_N
  #define WF_W GEMVF24_W
  #define WF_TAG "ap_fixed<24,12>"
  #define WF_SET(dst, src) dst.range() = (ap_uint<GEMVF24_W>)(src)
  #define WF_GET(v) ((long long)(v).range().to_uint64())
#endif

int main(int argc, char** argv) {
    const char* in_path = (argc > 1) ? argv[1] : "data/input.txt";
    const char* out_path = (argc > 2) ? argv[2] : "results/output.txt";

    int n_case = 0;
    std::vector<std::vector<long long> > As, xs;
    if (!read_int_cases(in_path, WF_M, WF_N, n_case, As, xs)) return 1;

    FILE* fo = fopen(out_path, "w");
    if (!fo) { printf("WAVEFLOW_ERROR: cannot open output %s\n", out_path); return 1; }
    fprintf(fo, "# Vitis BLAS gemv verification output -- %s\n", WF_TAG);
#ifdef WF_AS_VALUE
    // Integer DUTs emit the VALUE, not the stored bits.  int32_t and ap_uint<32> are the same
    // hardware and produce the same bits, so a bit-level comparison cannot tell a signed model
    // from an unsigned one -- it would pass either way, and the u32 DUT would prove nothing.
    // The decimal value is where the two genuinely differ.
    fprintf(fo, "# y is the signed-or-unsigned VALUE in decimal; logParEntries=%d\n", GEMVI_LOGP);
    fprintf(fo, "# columns: case row y_value\n");
#else
    fprintf(fo, "# y is the RAW STORED FIELD in hex; logParEntries=%d\n", GEMVI_LOGP);
    fprintf(fo, "# columns: case row y_hex\n");
#endif
    fprintf(fo, "# W n_cases M N logP\n%d %d %d %d %d\n", WF_W, n_case, WF_M, WF_N, GEMVI_LOGP);

    for (int k = 0; k < n_case; k++) {
        std::vector<WF_T> a(WF_M * WF_N), v(WF_N), y(WF_M);
        for (int i = 0; i < WF_M * WF_N; i++) WF_SET(a[i], As[k][i]);
        for (int j = 0; j < WF_N; j++)        WF_SET(v[j], xs[k][j]);
        WF_TOP(a.data(), v.data(), y.data());
        for (int r = 0; r < WF_M; r++) {
#ifdef WF_AS_VALUE
            fprintf(fo, "%d %d %lld\n", k, r, WF_GET(y[r]));
#else
            unsigned long long bits =
                (unsigned long long)WF_GET(y[r]) & ((WF_W >= 64) ? ~0ULL : ((1ULL << WF_W) - 1));
            fprintf(fo, "%d %d %llx\n", k, r, bits);
#endif
        }
    }
    fclose(fo);
    printf("WAVEFLOW_OK: wrote %d cases x %d rows to %s\n", n_case, WF_M, out_path);
    return 0;
}
