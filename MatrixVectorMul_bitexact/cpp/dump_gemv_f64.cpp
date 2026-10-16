// dump_gemv_f64.cpp -- golden for the DOUBLE element type.
//
// double takes the same dot_tree path as float, but AdderDelay<double> is 8 rather than 4, so it
// groups beats by 8 and the reduction has a different shape.  Every rounding also happens at 53
// bits instead of 24.
//
// This exists because the model USED to hardcode float32 while `adder_delays` already answered 8
// for float64 -- so asking for double returned plausible, silently wrong numbers.  A golden is
// the only thing that would have caught that, which is the argument for having one.
//
// Values are IEEE-754 bit patterns (64-bit), never decimals.
//
//   argv[1] = input file, argv[2] = output file
#include <hls_stream.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
#include "xf_blas.hpp"

using namespace xf::blas;

static double d_of(unsigned long long b) { double d; memcpy(&d, &b, 8); return d; }
static unsigned long long b_of(double d) { unsigned long long b; memcpy(&b, &d, 8); return b; }

template <int LOGP>
static void run_one(FILE* fo, const std::vector<std::vector<double> >& As,
                    const std::vector<std::vector<double> >& xs, int M, int N, int n_case) {
    const int P = 1 << LOGP;
    std::vector<double> y(M);
    for (int k = 0; k < n_case; k++) {
        std::vector<double> A = As[k], x = xs[k];
        hls::stream<typename WideType<double, P>::t_TypeInt> sA, sX;
        hls::stream<typename WideType<double, 1>::t_TypeInt> sY;
        gem2Stream<double, P>(M, N, A.data(), sA);
        vec2GemStream<double, P>(M, N, x.data(), sX);
        gemv<double, LOGP, unsigned int>(M, N, sA, sX, sY);
        writeStream2Vec<double, 1>(sY, M, y.data());
        for (int r = 0; r < M; r++) fprintf(fo, "%d %d %d %016llx\n", LOGP, k, r, b_of(y[r]));
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
    int n_case, M, N;
    if (fscanf(fi, "%d %d %d", &n_case, &M, &N) != 3) { printf("WF_ERROR: bad header\n"); return 1; }
    std::vector<std::vector<double> > As(n_case), xs(n_case);
    unsigned long long u;
    for (int k = 0; k < n_case; k++) {
        As[k].resize(M * N); xs[k].resize(N);
        for (int i = 0; i < M * N; i++) { if (fscanf(fi, "%llu", &u) != 1) return 1; As[k][i] = d_of(u); }
        for (int j = 0; j < N; j++)     { if (fscanf(fi, "%llu", &u) != 1) return 1; xs[k][j] = d_of(u); }
    }
    fclose(fi);

    FILE* fo = fopen(argv[2], "w");
    fprintf(fo, "# Vitis BLAS gemv golden -- DOUBLE (dot_tree, AdderDelay=%u)\n",
            AdderDelay<double>::m_Delays);
    fprintf(fo, "# IEEE-754 64-bit patterns, hex\n");
    fprintf(fo, "# columns: logParEntries case row y_bits\n");
    fprintf(fo, "# n_cases M N\n%d %d %d\n", n_case, M, N);
    run_one<1>(fo, As, xs, M, N, n_case);
    run_one<2>(fo, As, xs, M, N, n_case);
    run_one<3>(fo, As, xs, M, N, n_case);
    fclose(fo);
    printf("WF_OK: 3 widths x %d cases x %d rows -> %s\n", n_case, M, argv[2]);
    return 0;
}
