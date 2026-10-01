// dump_gemv_int.cpp -- golden for the NON-float gemv path (dot_dsp).
//
// float/double dispatch to dot_tree; every other element type goes to dot_dsp, which is a plain
// SEQUENTIAL accumulation in index order into a t_MacDataType accumulator -- no tree at all.
// So the risk here is not summation order but the accumulator's width and wrapping.
//
// t_MacDataType is NOT swept, because it cannot be: gemv declares its output stream as
// WideType<t_DataType,1> while forwarding to a DotHelper parameterised on t_MacDataType, so any
// t_MacDataType != t_DataType fails to compile INSIDE gemv.hpp:47.  The parameter is exposed and
// dead.  Identical in 2023.1 and 2025.1, so long-standing rather than a regression.  See PLAN.md.
//
// So the sweep is element type x logParEntries at the default MAC, and the inputs deliberately
// overflow the accumulator -- with the MAC pinned to the element width, wrapping is the behaviour
// a model is most likely to get wrong and least likely to hit by accident.
//
//   argv[1] = input file (int32 decimal values), argv[2] = output file
#include <hls_stream.h>
#include <cstdio>
#include <cstdlib>
#include <vector>
#include "xf_blas.hpp"

using namespace xf::blas;

template <typename T_elem, typename T_mac, int LOGP>
static void run_one(FILE* fo, const char* name,
                    const std::vector<long long>& A, const std::vector<long long>& x,
                    int M, int N, int n_case) {
    const int P = 1 << LOGP;
    std::vector<T_elem> a(M * N), v(N);
    std::vector<T_mac> y(M);
    for (int k = 0; k < n_case; k++) {
        for (int i = 0; i < M * N; i++) a[i] = (T_elem)A[k * M * N + i];
        for (int j = 0; j < N; j++) v[j] = (T_elem)x[k * N + j];
        hls::stream<typename WideType<T_elem, P>::t_TypeInt> sA, sX;
        hls::stream<typename WideType<T_mac, 1>::t_TypeInt> sY;
        gem2Stream<T_elem, P>(M, N, a.data(), sA);
        vec2GemStream<T_elem, P>(M, N, v.data(), sX);
        gemv<T_elem, LOGP, unsigned int, T_mac>(M, N, sA, sX, sY);
        writeStream2Vec<T_mac, 1>(sY, M, y.data());
        for (int r = 0; r < M; r++)
            fprintf(fo, "%s %d %d %d %lld\n", name, LOGP, k, r, (long long)y[r]);
    }
}

int main(int argc, char** argv) {
    FILE* fi = fopen(argv[1], "r");
    if (!fi) { printf("WF_ERROR: cannot open %s\n", argv[1]); return 1; }
    int c;
    while ((c = fgetc(fi)) != EOF) {
        if (c == '#') { while ((c = fgetc(fi)) != EOF && c != '\n') {} }
        else if (c != '\n' && c != ' ' && c != '\r') { ungetc(c, fi); break; }
    }
    int n_case, M, N;
    if (fscanf(fi, "%d %d %d", &n_case, &M, &N) != 3) { printf("WF_ERROR: bad header\n"); return 1; }
    std::vector<long long> A(n_case * M * N), x(n_case * N);
    for (int k = 0; k < n_case; k++) {
        for (int i = 0; i < M * N; i++) if (fscanf(fi, "%lld", &A[k * M * N + i]) != 1) return 1;
        for (int j = 0; j < N; j++)     if (fscanf(fi, "%lld", &x[k * N + j]) != 1) return 1;
    }
    fclose(fi);

    FILE* fo = fopen(argv[2], "w");
    fprintf(fo, "# Vitis BLAS gemv golden -- NON-float path (dot_dsp), decimal values\n");
    fprintf(fo, "# columns: config logParEntries case row y\n");
    fprintf(fo, "# n_cases M N\n%d %d %d\n", n_case, M, N);

    run_one<int32_t, int32_t, 2>(fo, "i32", A, x, M, N, n_case);
    run_one<int32_t, int32_t, 3>(fo, "i32", A, x, M, N, n_case);
    run_one<int32_t, int32_t, 4>(fo, "i32", A, x, M, N, n_case);
    run_one<int16_t, int16_t, 2>(fo, "i16", A, x, M, N, n_case);
    run_one<int16_t, int16_t, 3>(fo, "i16", A, x, M, N, n_case);
    // Unsigned and the width extremes.  The stored bits of ap_uint<32> and int32_t are identical
    // -- same hardware -- but the VALUE they denote is not, and a model that always reads the
    // accumulator as signed returns the negative counterpart of the right answer.  int8 and
    // int64 pin the ends of the width range; int64 also exercises the model's arbitrary-precision
    // accumulate, where a numpy int64 would wrap early and silently.
    run_one<ap_uint<32>, ap_uint<32>, 2>(fo, "u32", A, x, M, N, n_case);
    run_one<ap_uint<16>, ap_uint<16>, 2>(fo, "u16", A, x, M, N, n_case);
    run_one<int8_t, int8_t, 2>(fo, "i8", A, x, M, N, n_case);
    run_one<int64_t, int64_t, 2>(fo, "i64", A, x, M, N, n_case);
    fclose(fo);
    printf("WF_OK: non-float goldens written to %s\n", argv[2]);
    return 0;
}
