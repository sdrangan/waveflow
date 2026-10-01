// gemv_float_tb.cpp -- testbench for every plain (5-arg) FLOATING-POINT DUT.
//
// One source, compiled once per DUT with -DWF_DUT=<n>, because the four float/double DUTs differ
// only in element type and size.  Copies would drift.
//
// Shared by C-simulation and co-simulation; run.tcl passes different output paths so the two can
// be diffed against each other as well as against the Python model.
//
//   argv[1] = input file, argv[2] = output file
#include "gemv_top.hpp"
#include "gemv_tb_common.hpp"

#ifndef WF_DUT
#error "compile with -DWF_DUT=0..3"
#endif

#if WF_DUT == 0
  #define WF_T T_f32
  #define WF_BITS unsigned int
  #define WF_CONV f_of
  #define WF_TOP gemv_f32_top
  #define WF_M GEMV_M
  #define WF_N GEMV_N
  #define WF_LOGP GEMV_LOGP
  #define WF_TAG "float"
#elif WF_DUT == 1
  #define WF_T T_f32
  #define WF_BITS unsigned int
  #define WF_CONV f_of
  #define WF_TOP gemv_f32_wide_top
  #define WF_M GEMVW_M
  #define WF_N GEMVW_N
  #define WF_LOGP GEMVW_LOGP
  #define WF_TAG "float"
#elif WF_DUT == 2
  #define WF_T T_f32
  #define WF_BITS unsigned int
  #define WF_CONV f_of
  #define WF_TOP gemv_f32_pad_top
  #define WF_M GEMVPD_M
  #define WF_N GEMVPD_N
  #define WF_LOGP GEMVPD_LOGP
  #define WF_TAG "float"
#elif WF_DUT == 3
  #define WF_T T_f64
  #define WF_BITS unsigned long long
  #define WF_CONV d_of
  #define WF_TOP gemv_f64_top
  #define WF_M GEMVD_M
  #define WF_N GEMVD_N
  #define WF_LOGP GEMVD_LOGP
  #define WF_TAG "double"
#endif

int main(int argc, char** argv) {
    const char* in_path = (argc > 1) ? argv[1] : "data/input.txt";
    const char* out_path = (argc > 2) ? argv[2] : "results/output.txt";

    int n_case = 0;
    std::vector<std::vector<WF_T> > As, xs;
    if (!read_bit_cases<WF_T, WF_BITS, WF_CONV>(in_path, WF_M, WF_N, n_case, As, xs)) return 1;

    FILE* fo = fopen(out_path, "w");
    if (!fo) { printf("WAVEFLOW_ERROR: cannot open output %s\n", out_path); return 1; }
    fprintf(fo, "# Vitis BLAS gemv verification output -- %s, 5-arg overload\n", WF_TAG);
    fprintf(fo, "# IEEE-754 bit patterns (decimal); logParEntries=%d\n", WF_LOGP);
    fprintf(fo, "# columns: case row y_bits\n");
    fprintf(fo, "# n_cases M N logP\n%d %d %d %d\n", n_case, WF_M, WF_N, WF_LOGP);

    for (int k = 0; k < n_case; k++) {
        std::vector<WF_T> y(WF_M);
        WF_TOP(As[k].data(), xs[k].data(), y.data());
        for (int r = 0; r < WF_M; r++)
            fprintf(fo, "%d %d %llu\n", k, r, (unsigned long long)b_of(y[r]));
    }
    fclose(fo);
    printf("WAVEFLOW_OK: wrote %d cases x %d rows to %s\n", n_case, WF_M, out_path);
    return 0;
}
