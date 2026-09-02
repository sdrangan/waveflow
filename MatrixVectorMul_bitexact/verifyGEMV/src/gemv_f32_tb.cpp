// gemv_f32_tb.cpp -- testbench for the 5-arg float gemv, shared by C-sim and co-sim.
//
// The same binary serves both runs; run.tcl passes different output paths so the two can be
// diffed against each other as well as against the Python model.
//
//   argv[1] = input file, argv[2] = output file
#include <cstdlib>
#include <vector>
#include "gemv_top.hpp"
#include "gemv_tb_common.hpp"

using namespace xf::blas;

int main(int argc, char** argv) {
    const char* in_path = (argc > 1) ? argv[1] : "data/input_f32.txt";
    const char* out_path = (argc > 2) ? argv[2] : "results/output_f32.txt";

    FILE* fi = fopen(in_path, "r");
    if (!fi) { printf("WAVEFLOW_ERROR: cannot open input %s\n", in_path); return 1; }
    skip_comments(fi);
    int n_case, M, N;
    if (fscanf(fi, "%d %d %d", &n_case, &M, &N) != 3) {
        printf("WAVEFLOW_ERROR: bad header in %s\n", in_path); fclose(fi); return 1;
    }
    if (M != GEMV_M || N != GEMV_N) {
        printf("WAVEFLOW_ERROR: input is M=%d N=%d, DUT is M=%d N=%d\n", M, N, GEMV_M, GEMV_N);
        fclose(fi); return 1;
    }
    std::vector<std::vector<float> > As(n_case), xs(n_case);
    unsigned int u;
    for (int k = 0; k < n_case; k++) {
        As[k].resize(M * N); xs[k].resize(N);
        for (int i = 0; i < M * N; i++) {
            if (fscanf(fi, "%u", &u) != 1) { printf("WAVEFLOW_ERROR: short A\n"); return 1; }
            As[k][i] = f_of(u);
        }
        for (int j = 0; j < N; j++) {
            if (fscanf(fi, "%u", &u) != 1) { printf("WAVEFLOW_ERROR: short x\n"); return 1; }
            xs[k][j] = f_of(u);
        }
    }
    fclose(fi);

    FILE* fo = fopen(out_path, "w");
    if (!fo) { printf("WAVEFLOW_ERROR: cannot open output %s\n", out_path); return 1; }
    fprintf(fo, "# Vitis BLAS gemv verification output -- float, 5-arg overload\n");
    fprintf(fo, "# IEEE-754 bit patterns; logParEntries=%d\n", GEMV_LOGP);
    fprintf(fo, "# columns: case row y_bits\n");
    fprintf(fo, "# n_cases M N logP\n%d %d %d %d\n", n_case, M, N, GEMV_LOGP);

    for (int k = 0; k < n_case; k++) {
        std::vector<float> y(M);
        gemv_f32_top(As[k].data(), xs[k].data(), y.data());
        for (int r = 0; r < M; r++) fprintf(fo, "%d %d %u\n", k, r, b_of(y[r]));
    }
    fclose(fo);
    printf("WAVEFLOW_OK: wrote %d cases x %d rows to %s\n", n_case, M, out_path);
    return 0;
}
