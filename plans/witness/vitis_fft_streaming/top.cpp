// Streaming vs non-streaming connection of the Vitis SSR FFT (Vitis_Libraries dsp L1 user guide).
#include "types.hpp"

static void in_proc(hls::stream<T_in> src[FR], hls::stream<T_in> dst[FR]) {
    for (int i = 0; i < FL / FR; i++) {
#pragma HLS PIPELINE II = 1
        for (int r = 0; r < FR; r++) dst[r].write(src[r].read());
    }
}
static void out_proc(hls::stream<T_out> src[FR], hls::stream<T_out> dst[FR]) {
    for (int i = 0; i < FL / FR; i++) {
#pragma HLS PIPELINE II = 1
        for (int r = 0; r < FR; r++) dst[r].write(src[r].read());
    }
}

#ifdef STREAMING
// The guide's "Streaming Connection": innerFFT in a DATAFLOW region between producer and consumer.
void fft_top(hls::stream<T_in> inD[FR], hls::stream<T_out> outD[FR]) {
#pragma HLS DATAFLOW
    hls::stream<T_in> fft_in[FR];
    hls::stream<T_out> fft_out[FR];
#ifdef DEEP
#pragma HLS STREAM variable = fft_in depth = (FL / FR)
#pragma HLS STREAM variable = fft_out depth = (FL / FR)
#endif
#ifdef CHAIN
#pragma HLS INTERFACE ap_ctrl_chain port = return
#endif
    in_proc(inD, fft_in);
    FFTWrapper<(((ssrFFTLog2<FL>::val) % (ssrFFTLog2<FR>::val)) > 0), (FL) < ((FR * FR)),
               P::default_t_instanceID> core;
    core.template innerFFT<FL, FR, P::default_t_instanceID, P::scaling_mode, P::transform_direction,
                           P::butterfly_rnd_mode, P::output_data_order, T_exp, T_tw, T_in, T_out>(
        fft_in, fft_out);
    out_proc(fft_out, outD);
}
#else
// The guide's "Non-Streaming Connection": just call fft<> (what AMD's own L1 test top does).
void fft_top(hls::stream<T_in> inD[FR], hls::stream<T_out> outD[FR]) {
    fft<P>(inD, outD);
}
#endif
