// dump_gemv_fixed.cpp -- golden for gemv with an ap_fixed element type (S4).
//
// ap_fixed is not float, so DotHelper dispatches to dot_dsp: one accumulator, index order, no
// tree.  The accumulator is t_MacDataType, which S3 showed cannot differ from t_DataType, so it
// accumulates IN THE ELEMENT FORMAT -- every `l_res += ...` narrows back to <W,I> and applies the
// format's Q and O modes.  Hence the sweep below is over (QMode x OMode), which is where the bits
// are actually decided, rather than over accumulator widths, which the library will not compile.
//
// This same source produces BOTH goldens:
//
//   plain                 -> the shipped library, defect and all
//   -DWF_PATCHED -I...    -> cpp/vendor_patched/dotHelper_patched.hpp, whose include guard
//                            suppresses the shipped header and which differs from it by ONE line
//
// The defect: dot_dsp ends with `p_res.write(l_res)`, converting t_MacDataType to the stream's
// ap_uint<W> by VALUE.  Identity for an integer -- which is why S3 never saw it -- but for
// ap_fixed it truncates toward zero to the integer part, which the consumer then unpacks as a raw
// stored field.  See PLAN.md S4 and wf_gemv/fixed.py.
//
// Values in and out are STORED INTEGERS (the .range() field), never decimals: a decimal
// round-trip would insert a rounding step between the test data and the arithmetic under test.
//
//   argv[1] = input file, argv[2] = output file
#ifdef WF_PATCHED
#include "dotHelper_patched.hpp"
#endif
#include <hls_stream.h>
#include <ap_fixed.h>
#include <cstdio>
#include <cstdlib>
#include <vector>
#include "xf_blas.hpp"

using namespace xf::blas;

template <int W, int I, ap_q_mode Q, ap_o_mode O, int LOGP>
static void run_one(FILE* fo, const char* name,
                    const std::vector<long long>& A, const std::vector<long long>& x,
                    int M, int N, int n_case) {
    typedef ap_fixed<W, I, Q, O> T;
    const int P = 1 << LOGP;
    std::vector<T> a(M * N), v(N), y(M);
    for (int k = 0; k < n_case; k++) {
        for (int i = 0; i < M * N; i++) a[i].range() = (ap_uint<W>)A[k * M * N + i];
        for (int j = 0; j < N; j++)     v[j].range() = (ap_uint<W>)x[k * N + j];
        hls::stream<typename WideType<T, P>::t_TypeInt> sA, sX;
        hls::stream<typename WideType<T, 1>::t_TypeInt> sY;
        gem2Stream<T, P>(M, N, a.data(), sA);
        vec2GemStream<T, P>(M, N, v.data(), sX);
        gemv<T, LOGP, unsigned int>(M, N, sA, sX, sY);
        writeStream2Vec<T, 1>(sY, M, y.data());
        for (int r = 0; r < M; r++)
            fprintf(fo, "%s %d %d %d %llx\n", name, LOGP, k, r,
                    (unsigned long long)y[r].range().to_uint64());
    }
}

// The Q x O sweep at one width, at three stream widths.  logParEntries is swept even though
// dot_dsp accumulates in index order at any width -- so that "parEntries does not matter here"
// stays a measured fact rather than a remembered one.
template <int W, int I>
static void run_all(FILE* fo, const std::vector<long long>& A, const std::vector<long long>& x,
                    int M, int N, int n_case) {
#define WF_CASE(Q, O, NAME)                                                      \
    run_one<W, I, Q, O, 2>(fo, NAME, A, x, M, N, n_case);                        \
    run_one<W, I, Q, O, 3>(fo, NAME, A, x, M, N, n_case);                        \
    run_one<W, I, Q, O, 4>(fo, NAME, A, x, M, N, n_case);
    WF_CASE(AP_TRN, AP_WRAP, "trn_wrap")
    WF_CASE(AP_RND, AP_WRAP, "rnd_wrap")
    WF_CASE(AP_TRN, AP_SAT, "trn_sat")
    WF_CASE(AP_RND, AP_SAT, "rnd_sat")
#undef WF_CASE
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
    int W, I, n_case, M, N;
    if (fscanf(fi, "%d %d %d %d %d", &W, &I, &n_case, &M, &N) != 5) {
        printf("WF_ERROR: bad header\n"); return 1;
    }
    std::vector<long long> A(n_case * M * N), x(n_case * N);
    for (int k = 0; k < n_case; k++) {
        for (int i = 0; i < M * N; i++) if (fscanf(fi, "%lld", &A[k * M * N + i]) != 1) return 1;
        for (int j = 0; j < N; j++)     if (fscanf(fi, "%lld", &x[k * N + j]) != 1) return 1;
    }
    fclose(fi);

    FILE* fo = fopen(argv[2], "w");
#ifdef WF_PATCHED
    const char* build = "PATCHED -- dotHelper.hpp:98 writes through WideType (one-line fix)";
#else
    const char* build = "AS SHIPPED -- carries the dotHelper.hpp:98 ap_fixed defect";
#endif
    fprintf(fo, "# Vitis BLAS gemv golden -- ap_fixed<%d,%d>, %s\n", W, I, build);
    fprintf(fo, "# y is the RAW STORED FIELD in hex (the ap_uint<W> the kernel wrote)\n");
    fprintf(fo, "# columns: config logParEntries case row y_hex\n");
    fprintf(fo, "# W I n_cases M N\n%d %d %d %d %d\n", W, I, n_case, M, N);

    if (W == 16 && I == 8)        run_all<16, 8>(fo, A, x, M, N, n_case);
    else if (W == 24 && I == 12)  run_all<24, 12>(fo, A, x, M, N, n_case);
    else { printf("WF_ERROR: no instantiation for ap_fixed<%d,%d>\n", W, I); return 1; }
    fclose(fo);
    printf("WF_OK: ap_fixed goldens (%s) written to %s\n",
#ifdef WF_PATCHED
           "patched",
#else
           "as shipped",
#endif
           argv[2]);
    return 0;
}
