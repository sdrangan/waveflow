#ifndef WAVEFLOW_DSP_SSR_FFT_TASKS_H
#define WAVEFLOW_DSP_SSR_FFT_TASKS_H
// ssr_fft_tasks.h -- the free-running task bodies of waveflow.dsp.ssr_fft (plans/ssr_fft.md).
//
// Every task here is a `while (1)` at II = 1 that moves ONE R-lane word per iteration and never
// returns.  Frames are visible only as a counter, so frame k+1 follows frame k with no gap: that is
// the whole difference from the vendor library, whose processes are functions that must return once
// per frame (and so fill and drain once per frame).
//
// THE ARITHMETIC IS AMD'S, STATEMENT FOR STATEMENT.
//
// The bits are fixed by the Vitis L1 SSR FFT and modelled in waveflow.vitis_l1.fft.  ssr_cmul is the
// library's complexMultiply (hls_ssr_fft_complex_multiplier.hpp:29-45): every partial product is
// truncated into the FIRST operand's type before combining.  ssr_cacc is one adder-tree addition:
// wrap at the operand width, then convert into the accumulator.  Both are written with explicitly
// typed temporaries because that is where the quantization points are -- a "cleaner" expression
// would compute different bits.
//
// NO WORD IS PACKED BY HAND.  Each edge's E::read / E::write delegate to the generated
// <elem>_array_utils lane routines; the generated configuration header (waveflow.dsp.ssr_fft.hls)
// defines the edge structs.
//
// THE CONFIGURATION IS A TYPE.  A stage task takes a struct C (generated per stage):
//   C::in_e, C::out_e   edge adaptors: value_type, W, read(s, x[4]), write(s, y[4])
//   C::prod_t, acc1_t, acc2_t, out_t, tw_t   the stage's ap_fixed formats
//   C::ROTATE, C::MC    whether the stage rotates by twiddles; words per sub-transform
//   C::ex_re/ex_im(k)   the radix-4 constants W_4^k;  C::tw_re/tw_im(j)  the stage's twiddle ROM
// A commutator takes its edge E and its block size D.
//
// RESET.  Every body begins each tick with a blocking read (or, in the commutator, idles until data
// arrives), so nothing advances while the inputs are empty at reset; build with
// `config_rtl -reset state` anyway, since the statics are otherwise only power-on values.
#include <ap_fixed.h>
#include <ap_int.h>
#include <hls_stream.h>
#include <hls_streamofblocks.h>
#include <complex>

namespace ssr_fft {

static const int R = 4;

// The library's complexMultiply: partial products truncated into T1, combined in T1, then cast to TP.
template <typename T1, typename T2, typename TP>
inline std::complex<TP> ssr_cmul(const std::complex<T1>& a, const std::complex<T2>& b) {
#pragma HLS INLINE
    T1 r1 = a.real() * b.real();
    T1 r2 = a.imag() * b.imag();
    T1 rr = r1 - r2;
    T1 i1 = a.real() * b.imag();
    T1 i2 = a.imag() * b.real();
    T1 ii = i1 + i2;
    return std::complex<TP>(TP(rr), TP(ii));
}

// One adder-tree addition: wrap at the operand width, then convert into the target.
template <typename TO, typename TT>
inline std::complex<TT> ssr_cacc(const std::complex<TO>& a, const std::complex<TO>& b) {
#pragma HLS INLINE
    TO r = a.real() + b.real();
    TO i = a.imag() + b.imag();
    return std::complex<TT>(TT(r), TT(i));
}

// One radix-4 butterfly (lane p = input p), then -- for every stage but the last -- the rotation of
// output q by the twiddle W_L^(m q R^s), which the stage's ROM holds at index m*q.
template <typename C>
inline void ssr_butterfly(const typename C::in_e::value_type x[R], typename C::out_e::value_type y[R],
                          int m) {
#pragma HLS INLINE
    typedef typename C::in_e::value_type::value_type in_t;
    typedef typename C::prod_t prod_t;
    typedef typename C::acc1_t acc1_t;
    typedef typename C::acc2_t acc2_t;
    typedef typename C::out_t out_t;
    typedef typename C::tw_t tw_t;
    for (int q = 0; q < R; q++) {
#pragma HLS UNROLL
        std::complex<prod_t> p[R];
        for (int j = 0; j < R; j++) {
#pragma HLS UNROLL
            std::complex<tw_t> w(C::ex_re((q * j) % R), C::ex_im((q * j) % R));
            p[j] = ssr_cmul<in_t, tw_t, prod_t>(x[j], w);
        }
        std::complex<acc1_t> a = ssr_cacc<prod_t, acc1_t>(p[0], p[1]);
        std::complex<acc1_t> b = ssr_cacc<prod_t, acc1_t>(p[2], p[3]);
        std::complex<acc2_t> s = ssr_cacc<acc1_t, acc2_t>(a, b);
        std::complex<out_t> n(out_t(s.real()), out_t(s.imag()));
        if (C::ROTATE) {
            std::complex<tw_t> w(C::tw_re(m * q), C::tw_im(m * q));
            y[q] = ssr_cmul<out_t, tw_t, out_t>(n, w);
        } else {
            y[q] = n;
        }
    }
}

// A stage: one word in, one butterfly (+ rotation), one word out, every cycle.
template <typename C>
void ssr_fft_stage_task(hls::stream<ap_uint<C::in_e::W> >& s_in,
                        hls::stream<ap_uint<C::out_e::W> >& s_out) {
    static int m = 0;                          // butterfly index within the sub-transform
    while (1) {
#pragma HLS PIPELINE II = 1
        typename C::in_e::value_type x[R];
#pragma HLS ARRAY_PARTITION variable = x complete
        typename C::out_e::value_type y[R];
#pragma HLS ARRAY_PARTITION variable = y complete
        C::in_e::read(s_in, x);                 // BLOCKING, and first: see the reset note
        ssr_butterfly<C>(x, y, m);
        C::out_e::write(s_out, y);
        m = (m == C::MC - 1) ? 0 : m + 1;
    }
}

// A delay line of N ticks: read the oldest entry, write the newest in its place.
template <typename T, int N>
struct ssr_delay {
    T buf[N];
    bool ok[N];
    inline void shift(const T& x, bool xv, int p, T& y, bool& yv) {
#pragma HLS INLINE
        y = buf[p];
        yv = ok[p];
        buf[p] = x;
        ok[p] = xv;
    }
};

// Bits to hold 0 .. N-1 (at least 1).
template <int N>
struct ssr_bits {
    static const int value = (N <= 2) ? 1 : 1 + ssr_bits<(N + 1) / 2>::value;
};
template <>
struct ssr_bits<1> {
    static const int value = 1;
};

// The commutator: an R x R transpose of blocks of D words, at one word per tick.
//
// waveflow.dsp.ssr_fft.cycle_ref.CommutatorTask is this loop in Python, statement for statement,
// and is gated against the model's gather -- change one, change the other.
//
//   input triangle   lane j delayed j*D ticks
//   switch           output lane l takes input lane (k - l) mod R, k = slot = (cnt / D) mod R
//   output triangle  lane l delayed (R-1-l)*D ticks
//
// TICKS COME IN WHOLE GROUPS (R*D ticks): the slot is a function of the tick count, so each group
// is decided at its boundary -- data if a word is waiting; a bubble group if the last group was
// data (one is always enough to push a group's tail out); idle otherwise (no tick).  Nothing is
// written that was not read: output words carry the valid bit of the samples in them.
//
// The decision reads only `flush` and the input FIFO's empty flag.  It once read a count of the
// samples inside, decremented on every write -- which put the output path in the decision's timing
// path and cost 1.3 ns of slack at 250 MHz.
template <typename E, int D>
void ssr_fft_commutator_task(hls::stream<ap_uint<E::W> >& s_in, hls::stream<ap_uint<E::W> >& s_out) {
    typedef typename E::value_type T;
    typedef ap_uint<ssr_bits<R * D>::value> cnt_t;
    typedef ap_uint<ssr_bits<3 * D>::value> ptr_t;
    static ssr_delay<T, D> in1;                // input triangle: lanes 1..3
    static ssr_delay<T, 2 * D> in2;
    static ssr_delay<T, 3 * D> in3;
    static ssr_delay<T, 3 * D> out0;           // output triangle: lanes 0..2
    static ssr_delay<T, 2 * D> out1;
    static ssr_delay<T, D> out2;
#pragma HLS DEPENDENCE variable = in1.buf type = inter direction = RAW distance = D dependent = true
#pragma HLS DEPENDENCE variable = in1.ok type = inter direction = RAW distance = D dependent = true
#pragma HLS DEPENDENCE variable = out2.buf type = inter direction = RAW distance = D dependent = true
#pragma HLS DEPENDENCE variable = out2.ok type = inter direction = RAW distance = D dependent = true
#pragma HLS DEPENDENCE variable = in2.buf type = inter direction = RAW distance = 2 * D dependent = true
#pragma HLS DEPENDENCE variable = in2.ok type = inter direction = RAW distance = 2 * D dependent = true
#pragma HLS DEPENDENCE variable = out1.buf type = inter direction = RAW distance = 2 * D dependent = true
#pragma HLS DEPENDENCE variable = out1.ok type = inter direction = RAW distance = 2 * D dependent = true
#pragma HLS DEPENDENCE variable = in3.buf type = inter direction = RAW distance = 3 * D dependent = true
#pragma HLS DEPENDENCE variable = in3.ok type = inter direction = RAW distance = 3 * D dependent = true
#pragma HLS DEPENDENCE variable = out0.buf type = inter direction = RAW distance = 3 * D dependent = true
#pragma HLS DEPENDENCE variable = out0.ok type = inter direction = RAW distance = 3 * D dependent = true
    static ptr_t p1 = 0, p2 = 0, p3 = 0;       // the three delay lengths' pointers
    static cnt_t cnt = 0;                      // tick within the group, 0 .. R*D-1
    static bool data = false;                  // this group is data (else bubble, if ticking)
    static bool ticking = false;               // this group ticks at all (data or bubble)
    static bool flush = false;                 // the last group was data: a bubble group is owed

    while (1) {
#pragma HLS PIPELINE II = 1
        bool dt = data, tk = ticking;
        if (cnt == 0) {
            dt = !s_in.empty();
            tk = dt || flush;
            flush = dt;
        }
        data = dt;
        ticking = tk;
        if (tk) {
            T x[R];
#pragma HLS ARRAY_PARTITION variable = x complete
            if (dt) {
                E::read(s_in, x);
            } else {
                for (int j = 0; j < R; j++) x[j] = T();
            }

            T a[R], b[R], c[R];
            bool av[R], bv[R], cv[R];
#pragma HLS ARRAY_PARTITION variable = a complete
#pragma HLS ARRAY_PARTITION variable = b complete
#pragma HLS ARRAY_PARTITION variable = c complete
#pragma HLS ARRAY_PARTITION variable = av complete
#pragma HLS ARRAY_PARTITION variable = bv complete
#pragma HLS ARRAY_PARTITION variable = cv complete
            a[0] = x[0];
            av[0] = dt;
            in1.shift(x[1], dt, p1, a[1], av[1]);
            in2.shift(x[2], dt, p2, a[2], av[2]);
            in3.shift(x[3], dt, p3, a[3], av[3]);

            ap_uint<2> k = (ap_uint<2>)(cnt / D);
            for (int l = 0; l < R; l++) {
#pragma HLS UNROLL
                ap_uint<2> src = k - (ap_uint<2>)l;
                b[l] = a[src];
                bv[l] = av[src];
            }

            out0.shift(b[0], bv[0], p3, c[0], cv[0]);
            out1.shift(b[1], bv[1], p2, c[1], cv[1]);
            out2.shift(b[2], bv[2], p1, c[2], cv[2]);
            c[3] = b[3];
            cv[3] = bv[3];

            if (cv[0]) E::write(s_out, c);
            p1 = (p1 == D - 1) ? ptr_t(0) : ptr_t(p1 + 1);
            p2 = (p2 == 2 * D - 1) ? ptr_t(0) : ptr_t(p2 + 1);
            p3 = (p3 == 3 * D - 1) ? ptr_t(0) : ptr_t(p3 + 1);
            cnt = (cnt == R * D - 1) ? cnt_t(0) : cnt_t(cnt + 1);
        }
    }
}

// The digit-reversal reorder, second half: one frame of whole words into a stream_of_blocks block,
// at their natural addresses.  (The first half is an ordinary commutator, D = L/R^2, which has already
// put every bin on its final lane.)  Word b goes to word perm(b): keep the top base-R digit of b,
// reverse the other ND-1 -- waveflow.dsp.ssr_fft.model.reorder_word_perm.
//
// A single-firing body: the hls::task runtime re-fires it once per frame, and the RAII lock commits
// the block when it leaves scope (reference: examples/interleaver il_load_task.h).
template <typename E, int NW, int ND>
void ssr_fft_reorder_write_task(hls::stream<ap_uint<E::W> >& s_in,
                                hls::stream_of_blocks<ap_uint<E::W>[NW]>& m_blk) {
    hls::write_lock<ap_uint<E::W>[NW]> blk(m_blk);
    for (int b = 0; b < NW; b++) {
#pragma HLS PIPELINE II = 1
        ap_uint<2 * ND> bi = b, wi = 0;
        wi.range(2 * ND - 1, 2 * ND - 2) = bi.range(2 * ND - 1, 2 * ND - 2);
        for (int i = 0; i < ND - 1; i++) {
#pragma HLS UNROLL
            wi.range(2 * i + 1, 2 * i) = bi.range(2 * (ND - 2 - i) + 1, 2 * (ND - 2 - i));
        }
        blk[(int)wi] = s_in.read();
    }
}

// The reorder's reader: a filled block out, word by word, in natural order.
template <typename E, int NW>
void ssr_fft_reorder_read_task(hls::stream_of_blocks<ap_uint<E::W>[NW]>& s_blk,
                               hls::stream<ap_uint<E::W> >& s_out) {
    hls::read_lock<ap_uint<E::W>[NW]> blk(s_blk);
    for (int w = 0; w < NW; w++) {
#pragma HLS PIPELINE II = 1
        s_out.write(blk[w]);
    }
}

}  // namespace ssr_fft

#endif  // WAVEFLOW_DSP_SSR_FFT_TASKS_H
