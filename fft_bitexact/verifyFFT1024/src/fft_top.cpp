// fft_top.cpp -- synthesis top.  A thin wrapper so the DUT is one named function for
// csynth / cosim; all the work is the library's.
#include "fft_top.hpp"

void fft_top(hls::stream<T_in> p_in[FFT_R], hls::stream<T_out> p_out[FFT_R]) {
#pragma HLS TOP
    xf::dsp::fft::fft<fft_params>(p_in, p_out);
}
