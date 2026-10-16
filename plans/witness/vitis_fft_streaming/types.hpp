#pragma once
#include <ap_fixed.h>
#include <hls_stream.h>
#include <complex>
#include "vitis_fft/hls_ssr_fft.hpp"
using namespace xf::dsp::fft;
#define FL 1024
#define FR 4
struct P : ssr_fft_default_params {
    static const int N = FL;
    static const int R = 4;
    static const scaling_mode_enum scaling_mode = SSR_FFT_NO_SCALING;
    static const fft_output_order_enum output_data_order =
#ifdef DRT
        SSR_FFT_DIGIT_REVERSED_TRANSPOSED;
#else
        SSR_FFT_NATURAL;
#endif
    static const int twiddle_table_word_length = 18;
    static const int twiddle_table_intger_part_length = 2;
};
typedef std::complex<ap_fixed<16, 2> > T_in;
typedef FFTInputTraits<T_in>::T_castedType casted_type;
typedef FFTOutputTraits<FL, FR, P::scaling_mode, P::transform_direction, P::butterfly_rnd_mode,
                        casted_type>::T_FFTOutType T_out;
typedef InputBasedTwiddleTraits<P, casted_type>::T_twiddleType T_tw;
typedef InputBasedTwiddleTraits<P, casted_type>::T_expTabType T_exp;

void fft_top(hls::stream<T_in> inD[FR], hls::stream<T_out> outD[FR]);
