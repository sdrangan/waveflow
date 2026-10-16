// dump_gemv_ab.cpp -- golden for the alpha/beta gemv overload (S5).
//
//     yr = alpha * (M x) + beta * y
//
// The 8-arg gemv (gemv.hpp:67-85) is a composition of three shipped kernels, not new arithmetic:
//
//     gemv(...)  ->  l_x        the 5-arg overload, i.e. everything S1-S2 already models
//     scal(...)  ->  l_y        l_y[j] = beta * y[j]                (scal.hpp:65)
//     axpy(...)  ->  yr         yr[j]  = alpha * l_x[j] + l_y[j]    (axpy.hpp:71)
//
// The interesting line is axpy's `p_alpha * l_realX + l_realY`: a multiply feeding an add, in one
// expression, in floating point.  That is a fused-multiply-add candidate, and an FMA keeps the
// product's full precision where a separate mul+add rounds it -- so the two give different bits.
// This dumper therefore exists to be built at several optimization levels and compared, rather
// than to be trusted at one.  See tests/test_gemv_ab.py.
//
// Note that scal and axpy write through WideType, so they pack by bit pattern and do NOT carry
// the ap_fixed defect that S4 found in dot_dsp.  This overload inherits that defect only through
// its inner gemv call, and only for a non-float element type.
//
// Values are IEEE-754 BIT PATTERNS in and out.
//
//   argv[1] = input file, argv[2] = output file
#include <hls_stream.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
#include "xf_blas.hpp"

using namespace xf::blas;

static float f_of(unsigned int b) { float f; memcpy(&f, &b, 4); return f; }
static unsigned int b_of(float f) { unsigned int b; memcpy(&b, &f, 4); return b; }

template <int LOGP>
static void run_one(FILE* fo, const std::vector<std::vector<float> >& As,
                    const std::vector<std::vector<float> >& xs,
                    const std::vector<std::vector<float> >& ys,
                    const std::vector<float>& alphas, const std::vector<float>& betas,
                    int M, int N, int n_case) {
    const int P = 1 << LOGP;
    std::vector<float> yr(M);
    for (size_t t = 0; t < alphas.size(); t++) {
        for (int k = 0; k < n_case; k++) {
            std::vector<float> A = As[k], x = xs[k], y = ys[k];
            hls::stream<typename WideType<float, P>::t_TypeInt> sA, sX;
            hls::stream<typename WideType<float, 1>::t_TypeInt> sY, sYr;
            gem2Stream<float, P>(M, N, A.data(), sA);
            vec2GemStream<float, P>(M, N, x.data(), sX);
            // y enters as a width-1 packed stream, the same shape the result leaves in
            for (int r = 0; r < M; r++) {
                WideType<float, 1> w;
                w[0] = y[r];
                sY.write(w);
            }
            gemv<float, LOGP, unsigned int>(M, N, alphas[t], sA, sX, betas[t], sY, sYr);
            writeStream2Vec<float, 1>(sYr, M, yr.data());
            for (int r = 0; r < M; r++)
                fprintf(fo, "%zu %d %d %d %u\n", t, LOGP, k, r, b_of(yr[r]));
        }
    }
}

int main(int argc, char** argv) {
    if (argc < 3) { printf("WF_ERROR: usage: %s <in> <out>\n", argv[0]); return 1; }
    FILE* fi = fopen(argv[1], "r");
    if (!fi) { printf("WF_ERROR: cannot open %s\n", argv[1]); return 1; }
    int c;
    while ((c = fgetc(fi)) != EOF) {
        if (c == '#') { while ((c = fgetc(fi)) != EOF && c != '\n') {} }
        else if (c != '\n' && c != ' ' && c != '\r') { ungetc(c, fi); break; }
    }
    int n_case, M, N, n_ab;
    if (fscanf(fi, "%d %d %d %d", &n_case, &M, &N, &n_ab) != 4) {
        printf("WF_ERROR: bad header\n"); return 1;
    }
    unsigned int u;
    std::vector<std::vector<float> > As(n_case), xs(n_case), ys(n_case);
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

    FILE* fo = fopen(argv[2], "w");
    fprintf(fo, "# Vitis BLAS gemv golden -- alpha/beta overload, yr = alpha*(M x) + beta*y\n");
    fprintf(fo, "# IEEE-754 bit patterns\n");
    fprintf(fo, "# columns: ab_index logParEntries case row yr_bits\n");
    fprintf(fo, "# n_cases M N n_ab\n%d %d %d %d\n", n_case, M, N, n_ab);
    run_one<2>(fo, As, xs, ys, alphas, betas, M, N, n_case);
    run_one<3>(fo, As, xs, ys, alphas, betas, M, N, n_case);
    run_one<4>(fo, As, xs, ys, alphas, betas, M, N, n_case);
    fclose(fo);
    printf("WF_OK: %d (alpha,beta) x 3 widths x %d cases x %d rows -> %s\n",
           n_ab, n_case, M, argv[2]);
    return 0;
}
