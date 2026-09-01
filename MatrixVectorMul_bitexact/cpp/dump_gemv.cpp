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

#ifndef GEMV_LOGP
#define GEMV_LOGP 2
#endif
#define GEMV_P (1 << GEMV_LOGP)

static float f_of(unsigned int b) { float f; memcpy(&f, &b, 4); return f; }
static unsigned int b_of(float f) { unsigned int b; memcpy(&b, &f, 4); return b; }

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
    if (N % GEMV_P) { printf("WF_ERROR: N=%d not a multiple of P=%d\n", N, GEMV_P); return 1; }

    FILE* fo = fopen(out_path, "w");
    if (!fo) { printf("WF_ERROR: cannot open %s\n", out_path); return 1; }
    fprintf(fo, "# Vitis BLAS gemv golden -- IEEE-754 bit patterns, one y value per line\n");
    fprintf(fo, "# logParEntries=%d  parEntries=%d  AdderDelay<float>=%d\n",
            GEMV_LOGP, GEMV_P, AdderDelay<float>::m_Delays);
    fprintf(fo, "# n_cases M N\n%d %d %d\n", n_case, M, N);

    std::vector<float> A(M * N), x(N), y(M);
    for (int k = 0; k < n_case; k++) {
        unsigned int u;
        for (int i = 0; i < M * N; i++) { if (fscanf(fi, "%u", &u) != 1) { printf("WF_ERROR: short A\n"); return 1; } A[i] = f_of(u); }
        for (int j = 0; j < N; j++)     { if (fscanf(fi, "%u", &u) != 1) { printf("WF_ERROR: short x\n"); return 1; } x[j] = f_of(u); }

        hls::stream<WideType<float, GEMV_P>::t_TypeInt> sA, sX;
        hls::stream<WideType<float, 1>::t_TypeInt> sY;
        gem2Stream<float, GEMV_P>(M, N, A.data(), sA);
        vec2GemStream<float, GEMV_P>(M, N, x.data(), sX);
        gemv<float, GEMV_LOGP, unsigned int, float>(M, N, sA, sX, sY);
        writeStream2Vec<float, 1>(sY, M, y.data());

        for (int r = 0; r < M; r++) fprintf(fo, "%u\n", b_of(y[r]));
    }
    fclose(fi); fclose(fo);
    printf("WF_OK: %d cases x %d rows written to %s\n", n_case, M, out_path);
    return 0;
}
