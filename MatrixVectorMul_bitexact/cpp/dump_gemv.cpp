// dump_gemv.cpp -- golden generator for the Vitis BLAS L1 gemv.
//
// Instantiates xf::blas::gemv from the shipped library and dumps what IT produces; nothing here
// reimplements the kernel.  Compiles natively under g++ against <hls_stream.h>, so a golden costs
// milliseconds rather than a Vitis run.
//
// Values are IEEE-754 BIT PATTERNS, never decimals: this kernel's whole difficulty is 1-ULP
// differences from summation order, and a decimal round-trip hides exactly those.
//
// The vector stream is fed with the library's own vec2GemStream, which repeats x for every row.
// Hand-feeding x instead makes gemv block forever on an empty stream rather than erroring.
//
//   argv[1] = input file   (see data format in ../README.md)
//   argv[2] = output file
#include <hls_stream.h>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <vector>
#include "xf_blas.hpp"

using namespace xf::blas;

// S1 showed t_LogParEntries changes the bits, so the golden sweeps logParEntries 0..4 rather
// than fixing one value.  N must be a multiple of the widest (16), which the generator ensures.

static float f_of(unsigned int b) { float f; memcpy(&f, &b, 4); return f; }
static unsigned int b_of(float f) { unsigned int b; memcpy(&b, &f, 4); return b; }

// One instantiation per logParEntries.  Recursive because the width is a template parameter and
// the sweep must cover several of them in one binary.
template <int LOGP>
static void run_one(FILE* fo, const std::vector<std::vector<float> >& As,
                    const std::vector<std::vector<float> >& xs, int M, int N, int n_case) {
    const int P = 1 << LOGP;
    std::vector<float> y(M);
    for (int k = 0; k < n_case; k++) {
        std::vector<float> A = As[k], x = xs[k];
        hls::stream<typename WideType<float, P>::t_TypeInt> sA, sX;
        hls::stream<typename WideType<float, 1>::t_TypeInt> sY;
        gem2Stream<float, P>(M, N, A.data(), sA);
        vec2GemStream<float, P>(M, N, x.data(), sX);
        gemv<float, LOGP, unsigned int, float>(M, N, sA, sX, sY);
        writeStream2Vec<float, 1>(sY, M, y.data());
        for (int r = 0; r < M; r++) fprintf(fo, "%u\n", b_of(y[r]));
    }
}

int main(int argc, char** argv) {
    const char* in_path = (argc > 1) ? argv[1] : "data/input.txt";
    const char* out_path = (argc > 2) ? argv[2] : "results/output.txt";

    FILE* fi = fopen(in_path, "r");
    if (!fi) { printf("WF_ERROR: cannot open %s\n", in_path); return 1; }
    int c;
    while ((c = fgetc(fi)) != EOF) {
        if (c == '#') { while ((c = fgetc(fi)) != EOF && c != '\n') {} }
        else if (c != '\n' && c != ' ' && c != '\r') { ungetc(c, fi); break; }
    }
    int n_case = 0, M = 0, N = 0;
    if (fscanf(fi, "%d %d %d", &n_case, &M, &N) != 3) {
        printf("WF_ERROR: bad header\n"); fclose(fi); return 1;
    }

    FILE* fo = fopen(out_path, "w");
    if (!fo) { printf("WF_ERROR: cannot open %s\n", out_path); return 1; }
    fprintf(fo, "# Vitis BLAS gemv golden -- IEEE-754 bit patterns\n");
    fprintf(fo, "# AdderDelay<float>=%d  AdderDelay<double>=%d\n",
            AdderDelay<float>::m_Delays, AdderDelay<double>::m_Delays);
    fprintf(fo, "# layout: for each logParEntries, for each case, M rows of y\n");
    fprintf(fo, "# n_logp logp_values... n_cases M N\n");

    // read every case up front: the sweep replays them at each width
    std::vector<std::vector<float> > As(n_case), xs(n_case);
    for (int k = 0; k < n_case; k++) {
        As[k].resize(M * N); xs[k].resize(N);
        unsigned int u;
        for (int i = 0; i < M * N; i++) { if (fscanf(fi, "%u", &u) != 1) { printf("WF_ERROR: short A\n"); return 1; } As[k][i] = f_of(u); }
        for (int j = 0; j < N; j++)     { if (fscanf(fi, "%u", &u) != 1) { printf("WF_ERROR: short x\n"); return 1; } xs[k][j] = f_of(u); }
    }
    fclose(fi);

    // Explicit, not a template recursion: the width is a compile-time parameter and five named
    // calls are clearer than machinery to generate them.  Keep this list and the header in step.
    fprintf(fo, "5 0 1 2 3 4 %d %d %d\n", n_case, M, N);
    run_one<0>(fo, As, xs, M, N, n_case);
    run_one<1>(fo, As, xs, M, N, n_case);
    run_one<2>(fo, As, xs, M, N, n_case);
    run_one<3>(fo, As, xs, M, N, n_case);
    run_one<4>(fo, As, xs, M, N, n_case);
    fclose(fo);
    printf("WF_OK: 5 widths x %d cases x %d rows -> %s\n", n_case, M, out_path);
    return 0;
}
