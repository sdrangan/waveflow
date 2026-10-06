#ifndef WAVEFLOW_BUILD_VITIS_FFT_TASK_H
#define WAVEFLOW_BUILD_VITIS_FFT_TASK_H
// vitis_fft_task.h -- the hand-written hls::task body for waveflow.vitis_l1.hw.VitisFft.
//
// Waveflow hands each AXI-Stream channel over as its own hls::stream of raw ap_uint words, while
// xf::dsp::fft::fft<> wants `hls::stream<T> p_in[R]` -- an ARRAY of streams, which cannot be
// formed from separate stream objects.  So this body is an adapter: R pump processes in, the
// vendor core, R pump processes out, all inside one DATAFLOW region.  That is the same shape the
// vendor's own L2 fftStreamingKernel uses (convertSuperStreamToArray -> fft<> -> back); it takes a
// single wide stream per side, which is not this module's port group, so the adaptation is ours.
//
// R is fixed at 4: it is the only radix the Python model covers, VitisFft refuses anything else,
// and a template cannot vary a function's arity anyway.
#include <ap_fixed.h>
#include <ap_int.h>
#include <hls_stream.h>
#include <complex>
#include "vitis_fft/hls_ssr_fft.hpp"

namespace vitis_fft_impl {

static const int WF_FFT_R = 4;

template <int L, int IN_W, int IN_I, int TW_W, int TW_I, int SCALING, int ORDER>
struct wf_fft_params : xf::dsp::fft::ssr_fft_default_params {
    static const int N = L;
    static const int R = WF_FFT_R;
    static const xf::dsp::fft::scaling_mode_enum scaling_mode =
        (xf::dsp::fft::scaling_mode_enum)SCALING;
    static const xf::dsp::fft::fft_output_order_enum output_data_order =
        (xf::dsp::fft::fft_output_order_enum)ORDER;
    static const xf::dsp::fft::transform_direction_enum transform_direction =
        xf::dsp::fft::FORWARD_TRANSFORM;
    static const xf::dsp::fft::butterfly_rnd_mode_enum butterfly_rnd_mode = xf::dsp::fft::TRN;
    static const int twiddle_table_word_length = TW_W;
    static const int twiddle_table_intger_part_length = TW_I;
};

// One lane in: raw word -> std::complex<ap_fixed>.  The real part occupies the LOW IN_W bits,
// matching std::complex's first member; the Python side packs the same way.
template <int L, int IN_W, int IN_I>
static void wf_fft_in_lane(hls::stream<ap_uint<2 * IN_W> >& src,
                           hls::stream<std::complex<ap_fixed<IN_W, IN_I> > >& dst) {
    for (int i = 0; i < L / WF_FFT_R; i++) {
#pragma HLS PIPELINE II = 1
        ap_uint<2 * IN_W> w = src.read();
        ap_fixed<IN_W, IN_I> re, im;
        re.range() = w.range(IN_W - 1, 0);
        im.range() = w.range(2 * IN_W - 1, IN_W);
        dst.write(std::complex<ap_fixed<IN_W, IN_I> >(re, im));
    }
}

template <int L, int OUT_W, typename T_out>
static void wf_fft_out_lane(hls::stream<T_out>& src, hls::stream<ap_uint<2 * OUT_W> >& dst) {
    for (int i = 0; i < L / WF_FFT_R; i++) {
#pragma HLS PIPELINE II = 1
        T_out v = src.read();
        ap_uint<2 * OUT_W> w = 0;
        w.range(OUT_W - 1, 0) = v.real().range();
        w.range(2 * OUT_W - 1, OUT_W) = v.imag().range();
        dst.write(w);
    }
}

// OUT_W is passed in rather than derived here, because it has to appear in the signature -- and
// that is a feature: the Python module derives the same number from the model, so the
// static_assert below turns "my derivation matches the vendor's OUTPUT_WL" into a COMPILE-TIME
// check instead of a claim.  A wrong width cannot reach synthesis.
template <int L, int IN_W, int IN_I, int TW_W, int TW_I, int SCALING, int ORDER, int OUT_W>
void vitis_fft_task(hls::stream<ap_uint<2 * IN_W> >& s_in_0,
                    hls::stream<ap_uint<2 * IN_W> >& s_in_1,
                    hls::stream<ap_uint<2 * IN_W> >& s_in_2,
                    hls::stream<ap_uint<2 * IN_W> >& s_in_3,
                    hls::stream<ap_uint<2 * OUT_W> >& m_out_0,
                    hls::stream<ap_uint<2 * OUT_W> >& m_out_1,
                    hls::stream<ap_uint<2 * OUT_W> >& m_out_2,
                    hls::stream<ap_uint<2 * OUT_W> >& m_out_3) {
    typedef wf_fft_params<L, IN_W, IN_I, TW_W, TW_I, SCALING, ORDER> P;
    typedef std::complex<ap_fixed<IN_W, IN_I> > T_in;
    typedef typename xf::dsp::fft::ssr_fft_output_type<P, T_in>::t_ssr_fft_out T_out;
    static_assert(OUT_W == (int)T_out::value_type::width,
                  "OUT_W disagrees with the vendor's ssr_fft_output_type: the Python module's "
                  "derived output width is wrong for this configuration.");

#pragma HLS DATAFLOW
    hls::stream<T_in> a_in[WF_FFT_R];
    hls::stream<T_out> a_out[WF_FFT_R];
#pragma HLS STREAM variable = a_in depth = (L / WF_FFT_R)
#pragma HLS STREAM variable = a_out depth = (L / WF_FFT_R)

    wf_fft_in_lane<L, IN_W, IN_I>(s_in_0, a_in[0]);
    wf_fft_in_lane<L, IN_W, IN_I>(s_in_1, a_in[1]);
    wf_fft_in_lane<L, IN_W, IN_I>(s_in_2, a_in[2]);
    wf_fft_in_lane<L, IN_W, IN_I>(s_in_3, a_in[3]);

    xf::dsp::fft::fft<P>(a_in, a_out);

    wf_fft_out_lane<L, OUT_W, T_out>(a_out[0], m_out_0);
    wf_fft_out_lane<L, OUT_W, T_out>(a_out[1], m_out_1);
    wf_fft_out_lane<L, OUT_W, T_out>(a_out[2], m_out_2);
    wf_fft_out_lane<L, OUT_W, T_out>(a_out[3], m_out_3);
}

}  // namespace vitis_fft_impl

// The generated top calls the body by its bare name (composite_gen predicts the RTL instance
// name from it, so it cannot carry a namespace qualifier); bring it into scope here.
using vitis_fft_impl::vitis_fft_task;
#endif
