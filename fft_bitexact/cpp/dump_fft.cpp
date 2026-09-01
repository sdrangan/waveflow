// dump_fft.cpp -- S2 golden: run the real Vitis L1 SSR FFT and dump input + output bits.
//
// Instantiates xf::dsp::fft::fft<> from the shipped library; nothing here reimplements the
// transform.  Compiled natively under g++ (hls::stream in csim mode is an ordinary queue), so
// producing a golden needs no Vitis run.
//
// Config = the default parameter struct except N/R: L=16, R=4, SSR_FFT_NO_SCALING,
// SSR_FFT_NATURAL, FORWARD_TRANSFORM, TRN butterflies, 18/2 twiddles.
//
// The input is defined by its STORED INTEGERS, not by doubles, so the Python model starts from
// bit-identical input and the comparison isolates the transform.
//
// Stream layout is the library's own testbench convention
// (L1/tests/hw/1dfft/.../main.cpp:110-113):  sample n -> stream (n % R) at time (n / R).
#include <ap_fixed.h>
#include <hls_stream.h>
#include <complex>
#include <cstdio>

#include "vitis_fft/hls_ssr_fft.hpp"

using namespace xf::dsp::fft;

static const int L = 16;
static const int R = 4;
static const int IN_W = 16, IN_I = 2;   // input ap_fixed<16,2>
static const int TW_W = 18, TW_I = 2;   // twiddle format (the default)

typedef ap_fixed<IN_W, IN_I> T_in_inner;
typedef std::complex<T_in_inner> T_in;

struct fft_params : ssr_fft_default_params {
    static const int N = L;
    static const int R = ::R;
    static const scaling_mode_enum scaling_mode = SSR_FFT_NO_SCALING;
    static const fft_output_order_enum output_data_order = SSR_FFT_NATURAL;
    static const transform_direction_enum transform_direction = FORWARD_TRANSFORM;
    static const butterfly_rnd_mode_enum butterfly_rnd_mode = TRN;
    static const int twiddle_table_word_length = TW_W;
    static const int twiddle_table_intger_part_length = TW_I;
};

typedef ssr_fft_output_type<fft_params, T_in>::t_ssr_fft_out T_out;

// Deterministic input, defined by stored bits so both sides start identical.
static int in_re_bits(int n) { return ((n * 2731 + 17) % (1 << IN_W)); }
static int in_im_bits(int n) { return ((n * 5417 + 913) % (1 << IN_W)); }

// NOTE: the HLS csim runtime prints "INFO [HLS SIM]: ..." to stdout when the program exits,
// which would corrupt JSON written there.  So the report goes to a FILE given by argv[1].
int main(int argc, char** argv) {
    FILE* out = (argc > 1) ? fopen(argv[1], "w") : stdout;
    if (!out) { fprintf(stderr, "cannot open %s\n", argv[1]); return 1; }
    hls::stream<T_in> din[R];
    hls::stream<T_out> dout[R];

    T_in x[L];
    for (int n = 0; n < L; n++) {
        T_in_inner re, im;
        re.range() = in_re_bits(n);
        im.range() = in_im_bits(n);
        x[n] = T_in(re, im);
    }
    for (int i = 0; i < L / R; i++)
        for (int j = 0; j < R; j++) din[j].write(x[i * R + j]);

    fft<fft_params>(din, dout);

    T_out y[L];
    for (int i = 0; i < L / R; i++)
        for (int j = 0; j < R; j++) y[i * R + j] = dout[j].read();

    typedef T_out::value_type T_out_inner;
    const int OUT_W = T_out_inner::width;
    const int OUT_I = T_out_inner::iwidth;

    fprintf(out, "{\n");
    fprintf(out, "  \"generator\": \"fft_bitexact/cpp/dump_fft.cpp\",\n");
    fprintf(out, "  \"source\": \"xf::dsp::fft::fft<fft_params>\",\n");
    fprintf(out, "  \"L\": %d, \"R\": %d,\n", L, R);
    fprintf(out, "  \"scaling_mode\": \"SSR_FFT_NO_SCALING\", \"output_order\": \"SSR_FFT_NATURAL\",\n");
    fprintf(out, "  \"transform_direction\": \"FORWARD_TRANSFORM\", \"butterfly_rnd_mode\": \"TRN\",\n");
    fprintf(out, "  \"in_W\": %d, \"in_I\": %d,\n", IN_W, IN_I);
    fprintf(out, "  \"tw_W\": %d, \"tw_I\": %d,\n", TW_W, TW_I);
    fprintf(out, "  \"out_W\": %d, \"out_I\": %d,\n", OUT_W, OUT_I);
    fprintf(out, "  \"note\": \"re/im are raw stored integers (unsigned two's complement of the width)\",\n");
    fprintf(out, "  \"stream_layout\": \"sample n -> stream (n %% R) at time (n / R)\",\n");

    fprintf(out, "  \"input\": [\n");
    for (int n = 0; n < L; n++)
        fprintf(out, "    {\"n\": %d, \"re\": %lld, \"im\": %lld}%s\n", n,
               (long long)x[n].real().range().to_int64(),
               (long long)x[n].imag().range().to_int64(), n == L - 1 ? "" : ",");
    fprintf(out, "  ],\n");

    fprintf(out, "  \"output\": [\n");
    for (int n = 0; n < L; n++)
        fprintf(out, "    {\"n\": %d, \"re\": %lld, \"im\": %lld, \"re_f\": %.17g, \"im_f\": %.17g}%s\n", n,
               (long long)y[n].real().range().to_int64(),
               (long long)y[n].imag().range().to_int64(),
               y[n].real().to_double(), y[n].imag().to_double(), n == L - 1 ? "" : ",");
    fprintf(out, "  ]\n}\n");
    if (out != stdout) fclose(out);
    return 0;
}
