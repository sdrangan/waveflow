// fft_top.hpp -- the design under test: AMD Vitis DSP L1 SSR FFT, fixed point.
//
// Configuration is the default parameter struct except N/R.  See ARCHITECTURE.md for what each
// setting means and which internal number formats it produces.
#ifndef FFT_TOP_HPP_
#define FFT_TOP_HPP_

#include <ap_fixed.h>
#include <hls_stream.h>
#include <complex>
#include "vitis_fft/hls_ssr_fft.hpp"

#define FFT_L 1024   // transform length
#define FFT_R 4       // radix / SSR factor  (L = R^5 -> 5 stages)
#define FFT_IN_W 16   // input  ap_fixed total bits
#define FFT_IN_I 2    // input  ap_fixed integer bits
#define FFT_TW_W 18   // twiddle table total bits
#define FFT_TW_I 2    // twiddle table integer bits

typedef ap_fixed<FFT_IN_W, FFT_IN_I> T_in_inner;
typedef std::complex<T_in_inner> T_in;

struct fft_params : xf::dsp::fft::ssr_fft_default_params {
    static const int N = FFT_L;
    static const int R = FFT_R;
    static const xf::dsp::fft::scaling_mode_enum scaling_mode = xf::dsp::fft::SSR_FFT_NO_SCALING;
    static const xf::dsp::fft::fft_output_order_enum output_data_order = xf::dsp::fft::SSR_FFT_NATURAL;
    static const xf::dsp::fft::transform_direction_enum transform_direction =
        xf::dsp::fft::FORWARD_TRANSFORM;
    static const xf::dsp::fft::butterfly_rnd_mode_enum butterfly_rnd_mode = xf::dsp::fft::TRN;
    static const int twiddle_table_word_length = FFT_TW_W;
    static const int twiddle_table_intger_part_length = FFT_TW_I;
};

typedef xf::dsp::fft::ssr_fft_output_type<fft_params, T_in>::t_ssr_fft_out T_out;

void fft_top(hls::stream<T_in> p_in[FFT_R], hls::stream<T_out> p_out[FFT_R]);

#endif
