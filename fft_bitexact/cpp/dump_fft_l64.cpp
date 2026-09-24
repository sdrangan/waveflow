// dump_fft_l64.cpp -- L=64 golden: three stages of recursion, the case that pins the
// narrow-then-rotate rule. Built against the PRISTINE library; writes JSON to argv[1].
#include <ap_fixed.h>
#include <hls_stream.h>
#include <complex>
#include <cstdio>
#include "vitis_fft/hls_ssr_fft.hpp"
using namespace xf::dsp::fft;
static const int L = 64, R = 4;
typedef ap_fixed<16, 2> T_i;
typedef std::complex<T_i> T_in;
struct P : ssr_fft_default_params {
    static const int N = L; static const int R = ::R;
    static const scaling_mode_enum scaling_mode = SSR_FFT_NO_SCALING;
    static const fft_output_order_enum output_data_order = SSR_FFT_NATURAL;
};
typedef ssr_fft_output_type<P, T_in>::t_ssr_fft_out T_out;
int main(int argc, char** argv) {
    FILE* f = (argc > 1) ? fopen(argv[1], "w") : stdout;
    const int NV = 6;
    fprintf(f, "{\"L\": %d, \"R\": %d, \"in_W\": 16, \"in_I\": 2, \"tw_W\": 18, \"tw_I\": 2,\n"
               " \"out_W\": %d, \"out_I\": %d,\n",
            L, R, (int)T_out::value_type::width, (int)T_out::value_type::iwidth);
    fprintf(f, " \"vectors\": [\n");
    for (int v = 0; v < NV; v++) {
        hls::stream<T_in> din[R]; hls::stream<T_out> dout[R];
        T_in x[L];
        for (int n = 0; n < L; n++) {
            T_i re, im;
            long long r, m;
            switch (v) {
                case 0: r = (n * 2731 + 17); m = (n * 5417 + 913); break;
                case 1: r = (long long)((n * 40503u + 20759u) * 2654435761u);
                        m = (long long)((n * 15485u + 7919u) * 40503u); break;
                case 2: r = (n == 0) ? (1 << 14) : 0; m = (n == 3) ? -(1 << 14) : 0; break;
                case 3: r = 12345; m = -9876; break;
                case 4: r = (n % 2) ? 32767 : -32768; m = (n % 2) ? -32768 : 32767; break;
                default: r = (n % 4 == 0) ? -32768 : (n * 8191);
                         m = (n % 4 == 1) ? 32767 : (n * 4093); break;
            }
            re.range() = (long long)(r & 0xFFFF); im.range() = (long long)(m & 0xFFFF);
            x[n] = T_in(re, im);
        }
        for (int i = 0; i < L / R; i++)
            for (int j = 0; j < R; j++) din[j].write(x[i * R + j]);
        fft<P>(din, dout);
        T_out y[L];
        for (int i = 0; i < L / R; i++)
            for (int j = 0; j < R; j++) y[i * R + j] = dout[j].read();
        fprintf(f, "  {\"v\": %d, \"input\": [", v);
        for (int n = 0; n < L; n++)
            fprintf(f, "{\"re\": %lld, \"im\": %lld}%s", (long long)x[n].real().range().to_int64(),
                    (long long)x[n].imag().range().to_int64(), n == L - 1 ? "" : ", ");
        fprintf(f, "], \"output\": [");
        for (int n = 0; n < L; n++)
            fprintf(f, "{\"re\": %lld, \"im\": %lld}%s", (long long)y[n].real().range().to_int64(),
                    (long long)y[n].imag().range().to_int64(), n == L - 1 ? "" : ", ");
        fprintf(f, "]}%s\n", v == NV - 1 ? "" : ",");
    }
    fprintf(f, " ]}\n");
    if (f != stdout) fclose(f);
    return 0;
}
