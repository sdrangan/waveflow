// dump_modes.cpp -- S4 goldens: the FFT under all three scaling modes, with per-stage traces.
//
// Same measure-first approach as S2: instrument, do not derive.  Emits, per scaling mode, the
// declared output format, every vector's output, and a full stage trace for two vectors (one
// in-range, one on the overflow boundary).
//
// Built against cpp/vendor_debug/ with -DWF_FFT_TRACE.  Writes JSON to argv[1].
#include <ap_fixed.h>
#include <hls_stream.h>
#include <complex>
#include <cstdio>
#include "wf_trace.hpp"
#include "vitis_fft/hls_ssr_fft.hpp"

using namespace xf::dsp::fft;
static const int L = 16, R = 4, IN_W = 16, IN_I = 2, TW_W = 18, TW_I = 2;
typedef ap_fixed<IN_W, IN_I> T_in_inner;
typedef std::complex<T_in_inner> T_in;

// Same 12 vectors as dump_fft.cpp -- kept in step by hand; test_modes asserts the inputs match.
static int re_bits(int v, int n) {
    switch (v) {
        case 0: return (n * 2731 + 17) % (1 << IN_W);
        case 1: return (int)((n * 40503u + 20759u) * 2654435761u % (1u << IN_W));
        case 2: return n == 0 ? (1 << (IN_W - 2)) : 0;
        case 3: return 12345;
        case 4: return (n % 2) ? ((1 << (IN_W - 1)) - 1) : (1 << (IN_W - 1));
        case 5: return (1 << (IN_W - 1));
        case 6: return ((1 << (IN_W - 1)) - 1);
        case 7: return (n < 8) ? (1 << (IN_W - 1)) : ((1 << (IN_W - 1)) - 1);
        case 8: return (int)((n * 22367u + 5u) * 65521u % (1u << IN_W));
        case 9: return (int)((n * 31337u + 4099u) * 2654435761u % (1u << IN_W));
        case 10: return (n % 4 == 0) ? (1 << (IN_W - 1)) : (n * 8191) % (1 << IN_W);
        default: return (n % 3 == 0) ? ((1 << (IN_W - 1)) - 1) : 0;
    }
}
static int im_bits(int v, int n) {
    switch (v) {
        case 0: return (n * 5417 + 913) % (1 << IN_W);
        case 1: return (int)((n * 15485u + 7919u) * 40503u % (1u << IN_W));
        case 2: return n == 3 ? -(1 << (IN_W - 2)) & ((1 << IN_W) - 1) : 0;
        case 3: return (1 << IN_W) - 9876;
        case 4: return (n % 2) ? (1 << (IN_W - 1)) : ((1 << (IN_W - 1)) - 1);
        case 5: return (1 << (IN_W - 1));
        case 6: return ((1 << (IN_W - 1)) - 1);
        case 7: return (n < 8) ? ((1 << (IN_W - 1)) - 1) : (1 << (IN_W - 1));
        case 8: return (int)((n * 7331u + 991u) * 40503u % (1u << IN_W));
        case 9: return (int)((n * 15013u + 733u) * 15485u % (1u << IN_W));
        case 10: return (n % 4 == 1) ? ((1 << (IN_W - 1)) - 1) : (n * 4093) % (1 << IN_W);
        default: return (n % 3 == 1) ? (1 << (IN_W - 1)) : 0;
    }
}
static const int N_VEC = 12;

template <scaling_mode_enum MODE>
struct P : ssr_fft_default_params {
    static const int N = L;
    static const int R = ::R;
    static const scaling_mode_enum scaling_mode = MODE;
    static const fft_output_order_enum output_data_order = SSR_FFT_NATURAL;
    static const transform_direction_enum transform_direction = FORWARD_TRANSFORM;
    static const butterfly_rnd_mode_enum butterfly_rnd_mode = TRN;
    static const int twiddle_table_word_length = TW_W;
    static const int twiddle_table_intger_part_length = TW_I;
};

template <scaling_mode_enum MODE>
static void run_mode(FILE* out, const char* name, bool last) {
    typedef typename ssr_fft_output_type<P<MODE>, T_in>::t_ssr_fft_out T_out;
    typedef typename T_out::value_type T_out_inner;

    fprintf(out, "    {\"mode\": \"%s\", \"out_W\": %d, \"out_I\": %d,\n",
            name, (int)T_out_inner::width, (int)T_out_inner::iwidth);
    fprintf(out, "     \"vectors\": [\n");
    for (int v = 0; v < N_VEC; v++) {
        hls::stream<T_in> din[R];
        hls::stream<T_out> dout[R];
        T_in x[L];
        for (int n = 0; n < L; n++) {
            T_in_inner re, im;
            re.range() = re_bits(v, n);
            im.range() = im_bits(v, n);
            x[n] = T_in(re, im);
        }
        for (int i = 0; i < L / R; i++)
            for (int j = 0; j < R; j++) din[j].write(x[i * R + j]);
        if (v == 1 || v == 4) wf_trace_clear();
        fft<P<MODE> >(din, dout);
        T_out y[L];
        for (int i = 0; i < L / R; i++)
            for (int j = 0; j < R; j++) y[i * R + j] = dout[j].read();

        fprintf(out, "       {\"v\": %d, \"input\": [", v);
        for (int n = 0; n < L; n++)
            fprintf(out, "{\"re\": %lld, \"im\": %lld}%s", (long long)x[n].real().range().to_int64(),
                    (long long)x[n].imag().range().to_int64(), n == L - 1 ? "" : ", ");
        fprintf(out, "], \"output\": [");
        for (int n = 0; n < L; n++)
            fprintf(out, "{\"re\": %lld, \"im\": %lld}%s", (long long)y[n].real().range().to_int64(),
                    (long long)y[n].imag().range().to_int64(), n == L - 1 ? "" : ", ");
        fprintf(out, "]");
        if (v == 1 || v == 4) {
            fprintf(out, ",\n        \"trace\": [\n");
            wf_trace_dump(out);
            fprintf(out, "        ]");
        }
        fprintf(out, "}%s\n", v == N_VEC - 1 ? "" : ",");
    }
    fprintf(out, "     ]}%s\n", last ? "" : ",");
}

int main(int argc, char** argv) {
    FILE* out = (argc > 1) ? fopen(argv[1], "w") : stdout;
    if (!out) return 1;
    fprintf(out, "{\n  \"generator\": \"fft_bitexact/cpp/dump_modes.cpp\",\n");
    fprintf(out, "  \"L\": %d, \"R\": %d, \"in_W\": %d, \"in_I\": %d, \"tw_W\": %d, \"tw_I\": %d,\n",
            L, R, IN_W, IN_I, TW_W, TW_I);
    fprintf(out, "  \"modes\": [\n");
    run_mode<SSR_FFT_NO_SCALING>(out, "SSR_FFT_NO_SCALING", false);
    run_mode<SSR_FFT_SCALE>(out, "SSR_FFT_SCALE", false);
    run_mode<SSR_FFT_GROW_TO_MAX_WIDTH>(out, "SSR_FFT_GROW_TO_MAX_WIDTH", true);
    fprintf(out, "  ]\n}\n");
    if (out != stdout) fclose(out);
    return 0;
}
