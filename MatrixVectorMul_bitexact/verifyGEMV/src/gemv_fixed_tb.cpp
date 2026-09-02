// gemv_fixed_tb.cpp -- testbench for the ap_fixed gemv, shared by C-sim and co-sim.
//
// This DUT is expected to produce WRONG answers, and that is the point.  dot_dsp ends with
// `p_res.write(l_res)`, converting t_MacDataType to the stream's ap_uint<W> by VALUE rather than
// by bit pattern (dotHelper.hpp:98): it truncates toward zero to the integer part, which the
// consumer then unpacks as a raw stored field.  For an integer element type that conversion is
// the identity, which is why the defect is invisible until ap_fixed is instantiated.
//
// Native C-simulation already shows this.  What synthesis adds is whether the RTL agrees --
// i.e. whether the defect is a csim artifact or is really in the hardware.
//
// Values in and out are STORED INTEGERS (the .range() field), never decimals.
//
//   argv[1] = input file, argv[2] = output file
#include <cstdlib>
#include <vector>
#include "gemv_top.hpp"
#include "gemv_tb_common.hpp"

using namespace xf::blas;

int main(int argc, char** argv) {
    const char* in_path = (argc > 1) ? argv[1] : "data/input_fixed.txt";
    const char* out_path = (argc > 2) ? argv[2] : "results/output_fixed.txt";

    FILE* fi = fopen(in_path, "r");
    if (!fi) { printf("WAVEFLOW_ERROR: cannot open input %s\n", in_path); return 1; }
    skip_comments(fi);
    int W, I, n_case, M, N;
    if (fscanf(fi, "%d %d %d %d %d", &W, &I, &n_case, &M, &N) != 5) {
        printf("WAVEFLOW_ERROR: bad header in %s\n", in_path); fclose(fi); return 1;
    }
    if (W != GEMVF_W || I != GEMVF_I || M != GEMVF_M || N != GEMVF_N) {
        printf("WAVEFLOW_ERROR: input is ap_fixed<%d,%d> M=%d N=%d, DUT is ap_fixed<%d,%d> "
               "M=%d N=%d\n", W, I, M, N, GEMVF_W, GEMVF_I, GEMVF_M, GEMVF_N);
        fclose(fi); return 1;
    }
    std::vector<std::vector<long long> > As(n_case), xs(n_case);
    for (int k = 0; k < n_case; k++) {
        As[k].resize(M * N); xs[k].resize(N);
        for (int i = 0; i < M * N; i++) if (fscanf(fi, "%lld", &As[k][i]) != 1) return 1;
        for (int j = 0; j < N; j++)     if (fscanf(fi, "%lld", &xs[k][j]) != 1) return 1;
    }
    fclose(fi);

    FILE* fo = fopen(out_path, "w");
    if (!fo) { printf("WAVEFLOW_ERROR: cannot open output %s\n", out_path); return 1; }
    fprintf(fo, "# Vitis BLAS gemv verification output -- ap_fixed<%d,%d>\n", W, I);
    fprintf(fo, "# y is the RAW STORED FIELD in hex; logParEntries=%d\n", GEMV_LOGP);
    fprintf(fo, "# columns: case row y_hex\n");
    fprintf(fo, "# W I n_cases M N logP\n%d %d %d %d %d %d\n", W, I, n_case, M, N, GEMV_LOGP);

    for (int k = 0; k < n_case; k++) {
        std::vector<T_fixed> a(M * N), v(N), y(M);
        for (int i = 0; i < M * N; i++) a[i].range() = (ap_uint<GEMVF_W>)As[k][i];
        for (int j = 0; j < N; j++)     v[j].range() = (ap_uint<GEMVF_W>)xs[k][j];
        gemv_fixed_top(a.data(), v.data(), y.data());
        for (int r = 0; r < M; r++)
            fprintf(fo, "%d %d %llx\n", k, r, (unsigned long long)y[r].range().to_uint64());
    }
    fclose(fo);
    printf("WAVEFLOW_OK: wrote %d cases x %d rows to %s\n", n_case, M, out_path);
    return 0;
}
