#include "rotate_ref.h"

static ap_int<16> quantize(ap_int<28> acc) {
#pragma HLS INLINE
#ifdef ROT_MUTANT_FLOOR
    ap_int<28> r = acc >> 8;                  // the grader's self-test: truncation
#else
    ap_int<28> r = (acc + 128) >> 8;          // Q.16 -> Q.8, round half up
#endif
    if (r > 32767) return 32767;
    if (r < -32768) return -32768;
    return (ap_int<16>)r;
}

template <int W, class T>
static void rot(hls::stream<T>& in, hls::stream<T>& out, ap_uint<8>& status) {
#pragma HLS INLINE
    const int PAIRS = W / 32;                 // (x, y) pairs per word
    status = 0;
    T h0 = in.read();
    ap_uint<64> hd = h0.data;
    ap_uint<16> len = hd(31, 16);
    ap_int<10> c, s;
    if (W == 32) {
        T h1 = in.read();
        ap_uint<64> h1d = h1.data;
        c = h1d(9, 0);
        s = h1d(25, 16);
    } else {
        c = hd(41, 32);
        s = hd(57, 48);
    }
    const int nwords = (len + PAIRS - 1) / PAIRS;
words:
    for (int i = 0; i < nwords; i++) {
#pragma HLS PIPELINE II = 1
        T w = in.read();
        ap_uint<64> d = w.data;
        ap_uint<64> o = 0;
        for (int p = 0; p < PAIRS; p++) {
#pragma HLS UNROLL
            ap_int<16> x = d(32 * p + 15, 32 * p);
            ap_int<16> y = d(32 * p + 31, 32 * p + 16);
            ap_int<28> xc = x * c;
            ap_int<28> ys = y * s;
            ap_int<28> xs = x * s;
            ap_int<28> yc = y * c;
#ifdef ROT_MUTANT_SIGN
            ap_int<16> x1 = quantize(xc + ys);    // the grader's self-test: a sign error
#else
            ap_int<16> x1 = quantize(xc - ys);
#endif
            ap_int<16> y1 = quantize(xs + yc);
            o(32 * p + 15, 32 * p) = (ap_uint<16>)x1;
            o(32 * p + 31, 32 * p + 16) = (ap_uint<16>)y1;
        }
        T r;
        r.data = o;
        r.keep = -1;
        r.strb = -1;
        r.last = (i == nwords - 1);
        out.write(r);
        if (w.last && i != nwords - 1) {
            status = 1;
            return;
        }
    }
}

void rot32(hls::stream<rot_word32_t>& in, hls::stream<rot_word32_t>& out, ap_uint<8>& status) {
#pragma HLS INTERFACE axis port = in
#pragma HLS INTERFACE axis port = out
#pragma HLS INTERFACE s_axilite port = status bundle = control
#pragma HLS INTERFACE s_axilite port = return bundle = control
    rot<32>(in, out, status);
}

void rot64(hls::stream<rot_word64_t>& in, hls::stream<rot_word64_t>& out, ap_uint<8>& status) {
#pragma HLS INTERFACE axis port = in
#pragma HLS INTERFACE axis port = out
#pragma HLS INTERFACE s_axilite port = status bundle = control
#pragma HLS INTERFACE s_axilite port = return bundle = control
    rot<64>(in, out, status);
}
