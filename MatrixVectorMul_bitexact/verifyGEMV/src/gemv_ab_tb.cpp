// gemv_ab_tb.cpp -- testbench for the alpha/beta overload, shared by C-sim and co-sim.
//
// ⚠️ This is the one that matters most.  `axpy` computes `p_alpha * l_realX + l_realY` as a
// SINGLE expression (axpy.hpp:71).  A fused multiply-add keeps the product's full precision and
// rounds once; a separate multiply and add round twice, and they give different bits -- 25 of
// 288 rows on the native goldens.  Whether Vitis HLS emits a fused or an unfused operator is a
// question only synthesis can answer, and this DUT is how it gets answered.
//
//   argv[1] = input file, argv[2] = output file
#include <cstdlib>
#include <vector>
#include "gemv_top.hpp"
#include "gemv_tb_common.hpp"

using namespace xf::blas;

int main(int argc, char** argv) {
    const char* in_path = (argc > 1) ? argv[1] : "data/input_ab.txt";
    const char* out_path = (argc > 2) ? argv[2] : "results/output_ab.txt";

    FILE* fi = fopen(in_path, "r");
    if (!fi) { printf("WAVEFLOW_ERROR: cannot open input %s\n", in_path); return 1; }
    skip_comments(fi);
    int n_case, M, N, n_ab;
    if (fscanf(fi, "%d %d %d %d", &n_case, &M, &N, &n_ab) != 4) {
        printf("WAVEFLOW_ERROR: bad header in %s\n", in_path); fclose(fi); return 1;
    }
    if (M != GEMV_M || N != GEMV_N) {
        printf("WAVEFLOW_ERROR: input is M=%d N=%d, DUT is M=%d N=%d\n", M, N, GEMV_M, GEMV_N);
        fclose(fi); return 1;
    }
    std::vector<std::vector<float> > As(n_case), xs(n_case), ys(n_case);
    unsigned int u;
    for (int k = 0; k < n_case; k++) {
        As[k].resize(M * N); xs[k].resize(N); ys[k].resize(M);
        for (int i = 0; i < M * N; i++) { if (fscanf(fi, "%u", &u) != 1) return 1; As[k][i] = f_of(u); }
        for (int j = 0; j < N; j++)     { if (fscanf(fi, "%u", &u) != 1) return 1; xs[k][j] = f_of(u); }
        for (int r = 0; r < M; r++)     { if (fscanf(fi, "%u", &u) != 1) return 1; ys[k][r] = f_of(u); }
    }
    std::vector<float> alphas(n_ab), betas(n_ab);
    for (int t = 0; t < n_ab; t++) {
        if (fscanf(fi, "%u", &u) != 1) return 1; alphas[t] = f_of(u);
        if (fscanf(fi, "%u", &u) != 1) return 1; betas[t] = f_of(u);
    }
    fclose(fi);

    FILE* fo = fopen(out_path, "w");
    if (!fo) { printf("WAVEFLOW_ERROR: cannot open output %s\n", out_path); return 1; }
    fprintf(fo, "# Vitis BLAS gemv verification output -- alpha/beta overload\n");
    fprintf(fo, "# yr = alpha*(M x) + beta*y ; IEEE-754 bit patterns; logParEntries=%d\n", GEMV_LOGP);
    fprintf(fo, "# columns: ab_index case row yr_bits\n");
    fprintf(fo, "# n_cases M N n_ab logP\n%d %d %d %d %d\n", n_case, M, N, n_ab, GEMV_LOGP);

    for (int t = 0; t < n_ab; t++) {
        for (int k = 0; k < n_case; k++) {
            std::vector<float> yr(M);
            gemv_ab_top(alphas[t], betas[t], As[k].data(), xs[k].data(), ys[k].data(),
                        yr.data());
            for (int r = 0; r < M; r++) fprintf(fo, "%d %d %d %u\n", t, k, r, b_of(yr[r]));
        }
    }
    fclose(fo);
    printf("WAVEFLOW_OK: wrote %d (alpha,beta) x %d cases x %d rows to %s\n",
           n_ab, n_case, M, out_path);
    return 0;
}
