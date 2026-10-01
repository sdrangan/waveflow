// gemv_user_tb.cpp -- testbench for the `user` DUT: YOUR data, not the shipped cases.
//
// Driven entirely by src/gemv_user_cfg.hpp, which make_user_dut.py generates from the header of
// data/user_input.txt.  Nothing here is specific to any element type; the KIND macro selects how
// values are read and written.
//
// Floating-point data is read and written as IEEE-754 BIT PATTERNS in decimal, and everything
// else as stored integers.  Never decimals-as-text: this kernel's whole difficulty is 1-ULP
// differences, and a "%.17g" round-trip absorbs exactly those.
//
//   argv[1] = input file, argv[2] = output file
#include "gemv_top.hpp"
#include "gemv_tb_common.hpp"

int main(int argc, char** argv) {
    const char* in_path = (argc > 1) ? argv[1] : "data/user_input.txt";
    const char* out_path = (argc > 2) ? argv[2] : "results/output_user.txt";

    int n_case = 0;
    std::vector<std::vector<T_user> > As, xs;

#if WF_USER_KIND == WF_KIND_FLOAT || WF_USER_KIND == WF_KIND_DOUBLE
  #if WF_USER_KIND == WF_KIND_FLOAT
    if (!read_bit_cases<T_user, unsigned int, f_of>(in_path, WF_USER_M, WF_USER_N, n_case, As, xs))
        return 1;
  #else
    if (!read_bit_cases<T_user, unsigned long long, d_of>(in_path, WF_USER_M, WF_USER_N,
                                                          n_case, As, xs))
        return 1;
  #endif
#else
    std::vector<std::vector<long long> > Ai, xi;
    if (!read_int_cases(in_path, WF_USER_M, WF_USER_N, n_case, Ai, xi)) return 1;
    As.resize(n_case); xs.resize(n_case);
    for (int k = 0; k < n_case; k++) {
        As[k].resize(WF_USER_M * WF_USER_N); xs[k].resize(WF_USER_N);
        for (int j = 0; j < WF_USER_M * WF_USER_N; j++)
  #if WF_USER_KIND == WF_KIND_FIXED
            As[k][j].range() = (ap_uint<WF_USER_W>)Ai[k][j];
  #else
            As[k][j] = (T_user)Ai[k][j];
  #endif
        for (int j = 0; j < WF_USER_N; j++)
  #if WF_USER_KIND == WF_KIND_FIXED
            xs[k][j].range() = (ap_uint<WF_USER_W>)xi[k][j];
  #else
            xs[k][j] = (T_user)xi[k][j];
  #endif
    }
#endif

    FILE* fo = fopen(out_path, "w");
    if (!fo) { printf("WAVEFLOW_ERROR: cannot open output %s\n", out_path); return 1; }
    fprintf(fo, "# Vitis BLAS gemv verification output -- USER DUT, type %s\n", WF_USER_TYPENAME);
#if WF_USER_KIND == WF_KIND_FLOAT || WF_USER_KIND == WF_KIND_DOUBLE
    fprintf(fo, "# y is the IEEE-754 BIT PATTERN in decimal; logParEntries=%d\n", WF_USER_LOGP);
#elif WF_USER_KIND == WF_KIND_FIXED
    fprintf(fo, "# y is the RAW STORED FIELD in decimal; logParEntries=%d\n", WF_USER_LOGP);
#else
    fprintf(fo, "# y is the signed-or-unsigned VALUE in decimal; logParEntries=%d\n", WF_USER_LOGP);
#endif
    fprintf(fo, "# columns: case row y\n");
    fprintf(fo, "# n_cases M N logP\n%d %d %d %d\n",
            n_case, WF_USER_M, WF_USER_N, WF_USER_LOGP);

    for (int k = 0; k < n_case; k++) {
        std::vector<T_user> y(WF_USER_M);
        gemv_user_top(As[k].data(), xs[k].data(), y.data());
        for (int r = 0; r < WF_USER_M; r++) {
#if WF_USER_KIND == WF_KIND_FLOAT || WF_USER_KIND == WF_KIND_DOUBLE
            fprintf(fo, "%d %d %llu\n", k, r, (unsigned long long)b_of(y[r]));
#elif WF_USER_KIND == WF_KIND_FIXED
            fprintf(fo, "%d %d %llu\n", k, r,
                    (unsigned long long)(y[r].range().to_uint64()
                                         & ((WF_USER_W >= 64) ? ~0ULL : ((1ULL << WF_USER_W) - 1))));
#elif WF_USER_KIND == WF_KIND_UINT
            fprintf(fo, "%d %d %llu\n", k, r, (unsigned long long)y[r].to_uint64());
#else
            fprintf(fo, "%d %d %lld\n", k, r, (long long)y[r]);
#endif
        }
    }
    fclose(fo);
    printf("WAVEFLOW_OK: wrote %d cases x %d rows to %s\n", n_case, WF_USER_M, out_path);
    return 0;
}
