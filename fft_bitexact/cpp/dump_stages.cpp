// dump_stages.cpp -- per-stage trace of the real Vitis FFT, via the instrumented header copy.
//
// Uses cpp/vendor_debug/ (a copy of the shipped headers plus WF_TRACE points), NOT the pristine
// tree, so the trace cannot perturb the goldens produced by the other dumpers.
// Build with -DWF_FFT_TRACE.  Writes JSON to argv[1]; stdout carries HLS csim noise.
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

// v4 from dump_fft.cpp -- the alternating-extremes vector that diverges.
static int re_bits(int n) { return (n % 2) ? ((1 << (IN_W - 1)) - 1) : (1 << (IN_W - 1)); }
static int im_bits(int n) { return (n % 2) ? (1 << (IN_W - 1)) : ((1 << (IN_W - 1)) - 1); }

int main(int argc, char** argv) {
    FILE* out = (argc > 1) ? fopen(argv[1], "w") : stdout;
    if (!out) return 1;
    hls::stream<T_in> din[R];
    hls::stream<T_out> dout[R];
    T_in x[L];
    for (int n = 0; n < L; n++) {
        T_in_inner re, im;
        re.range() = re_bits(n);
        im.range() = im_bits(n);
        x[n] = T_in(re, im);
    }
    for (int i = 0; i < L / R; i++)
        for (int j = 0; j < R; j++) din[j].write(x[i * R + j]);
    wf_trace_clear();
    fft<fft_params>(din, dout);
    T_out y[L];
    for (int i = 0; i < L / R; i++)
        for (int j = 0; j < R; j++) y[i * R + j] = dout[j].read();

    fprintf(out, "{\n  \"vector\": \"v4-alternating-extremes\",\n");
    fprintf(out, "  \"L\": %d, \"R\": %d, \"in_W\": %d, \"in_I\": %d,\n", L, R, IN_W, IN_I);
    fprintf(out, "  \"input\": [");
    for (int n = 0; n < L; n++)
        fprintf(out, "{\"re\": %lld, \"im\": %lld}%s", (long long)x[n].real().range().to_int64(),
                (long long)x[n].imag().range().to_int64(), n == L - 1 ? "" : ", ");
    fprintf(out, "],\n  \"output\": [");
    for (int n = 0; n < L; n++)
        fprintf(out, "{\"re\": %lld, \"im\": %lld}%s", (long long)y[n].real().range().to_int64(),
                (long long)y[n].imag().range().to_int64(), n == L - 1 ? "" : ", ");
    fprintf(out, "],\n  \"trace\": [\n");
    wf_trace_dump(out);
    fprintf(out, "  ]\n}\n");
    if (out != stdout) fclose(out);
    return 0;
}
