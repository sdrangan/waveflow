// dump_cxops.cpp -- goldens for the two complex primitives the butterfly is built from.
//
// (a) requantize: complex<ap_fixed<SRC_W,SRC_I>> -> complex<ap_fixed<DST_W,DST_I,AP_TRN,AP_WRAP>>
// (b) the library's OWN complexMultiply, which is NOT "full product then requantize":
//     it stores every partial product into T_op1 first (hls_ssr_fft_complex_multiplier.hpp:29-45).
//
// Writes JSON to argv[1] -- stdout carries HLS csim INFO noise.
#include <ap_fixed.h>
#include <complex>
#include <cstdio>
#include "vitis_fft/hls_ssr_fft_complex_multiplier.hpp"

static const int D_W = 16, D_I = 2;    // data format   (op1)
static const int T_W = 18, T_I = 2;    // twiddle format (op2)
static const int P_W = 18, P_I = 2;    // product format per FFTMultiplicationTraits (max/max)
static const int S_W = 33, S_I = 5;    // a wide source, for the plain requantize case

typedef ap_fixed<D_W, D_I> T_d;
typedef ap_fixed<T_W, T_I> T_t;
typedef ap_fixed<P_W, P_I, AP_TRN, AP_WRAP, 0> T_p;
typedef ap_fixed<S_W, S_I> T_s;

static long long bits(double_t) { return 0; }

int main(int argc, char** argv) {
    FILE* out = (argc > 1) ? fopen(argv[1], "w") : stdout;
    if (!out) return 1;
    const int N = 24;

    fprintf(out, "{\n  \"generator\": \"fft_bitexact/cpp/dump_cxops.cpp\",\n");
    fprintf(out, "  \"data\": {\"W\": %d, \"I\": %d}, \"twiddle\": {\"W\": %d, \"I\": %d},\n",
            D_W, D_I, T_W, T_I);
    fprintf(out, "  \"product\": {\"W\": %d, \"I\": %d, \"q\": \"AP_TRN\", \"o\": \"AP_WRAP\"},\n", P_W, P_I);
    fprintf(out, "  \"src\": {\"W\": %d, \"I\": %d},\n", S_W, S_I);

    // (a) plain requantize, wide -> narrow
    fprintf(out, "  \"requantize\": [\n");
    for (int n = 0; n < N; n++) {
        T_s re, im;
        re.range() = (long long)((n * 2654435761u) % (1ull << S_W));
        im.range() = (long long)((n * 40503u + 12345u) % (1ull << S_W));
        T_p qre = re, qim = im;                       // the ap_fixed cast under test
        fprintf(out, "    {\"n\": %d, \"src_re\": %lld, \"src_im\": %lld, \"dst_re\": %lld, \"dst_im\": %lld}%s\n",
                n, (long long)re.range().to_int64(), (long long)im.range().to_int64(),
                (long long)qre.range().to_int64(), (long long)qim.range().to_int64(),
                n == N - 1 ? "" : ",");
    }
    fprintf(out, "  ],\n");

    // (b) the library's complexMultiply
    fprintf(out, "  \"complex_multiply\": [\n");
    for (int n = 0; n < N; n++) {
        T_d are, aim; T_t bre, bim;
        are.range() = (n * 7919 + 13) % (1 << D_W);
        aim.range() = (n * 6271 + 907) % (1 << D_W);
        bre.range() = (n * 104729 + 31) % (1 << T_W);
        bim.range() = (n * 15485 + 733) % (1 << T_W);
        std::complex<T_d> a(are, aim);
        std::complex<T_t> b(bre, bim);
        std::complex<T_p> p;
        xf::dsp::fft::complexMultiply<T_d, T_t, T_p>(a, b, p);
        fprintf(out, "    {\"n\": %d, \"a_re\": %lld, \"a_im\": %lld, \"b_re\": %lld, \"b_im\": %lld,"
                     " \"p_re\": %lld, \"p_im\": %lld}%s\n", n,
                (long long)are.range().to_int64(), (long long)aim.range().to_int64(),
                (long long)bre.range().to_int64(), (long long)bim.range().to_int64(),
                (long long)p.real().range().to_int64(), (long long)p.imag().range().to_int64(),
                n == N - 1 ? "" : ",");
    }
    fprintf(out, "  ]\n}\n");
    if (out != stdout) fclose(out);
    return 0;
}
