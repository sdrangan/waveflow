// dump_twiddle.cpp -- emit the Vitis L1 SSR FFT twiddle table as raw stored integers.
//
// This is the GOLDEN generator for S1.  It does not reimplement the table: it instantiates
// xf::dsp::fft::TwiddleTable<> from the shipped Vitis DSP library and dumps what that code
// produces.  Compiled natively with g++ against the real <ap_fixed.h>, so producing a golden
// needs no Vitis run, no csim, and takes milliseconds.
//
// Output: JSON on stdout.  Values are the RAW STORED INTEGERS of the ap_fixed, not doubles --
// bit-exactness is a claim about stored bits, and a double round-trip could hide a 1-LSB error.
//
// Build: see ../tools/regen_golden.sh
#include <ap_fixed.h>
#include <complex>
#include <cstdio>
#include <cmath>

#include "vitis_fft/hls_ssr_fft_twiddle_table.hpp"

// The default parameter struct's twiddle format: hls_ssr_fft_enums.hpp:82-83.
static const int W = 18;  // twiddle_table_word_length
static const int I = 2;   // twiddle_table_intger_part_length ("+1/-1 stored correctly")

static const int L = 16;  // transform length for S1
static const int R = 4;   // radix

typedef ap_fixed<W, I> T_inner;                       // the table's declared element type
typedef std::complex<T_inner> T_cplx;

int main() {
    static const int EXT_LEN =
        xf::dsp::fft::TwiddleTableLENTraits<L, R>::EXTENDED_TWIDDLE_TALBE_LENGTH;

    static T_cplx table[EXT_LEN];
    xf::dsp::fft::TwiddleTable<L, R, 0, T_cplx>::initTwiddleTable(table);

    printf("{\n");
    printf("  \"generator\": \"fft_bitexact/cpp/dump_twiddle.cpp\",\n");
    printf("  \"source\": \"xf::dsp::fft::TwiddleTable<L,R,0,std::complex<ap_fixed<W,I>>>\",\n");
    printf("  \"L\": %d, \"R\": %d, \"W\": %d, \"I\": %d,\n", L, R, W, I);
    printf("  \"ext_len\": %d,\n", EXT_LEN);
    printf("  \"note\": \"values are raw stored integers of the ap_fixed, two's complement\",\n");
    printf("  \"table\": [\n");
    for (int i = 0; i < EXT_LEN; i++) {
        long long re = (long long)table[i].real().range().to_int64();
        long long im = (long long)table[i].imag().range().to_int64();
        printf("    {\"i\": %d, \"re\": %lld, \"im\": %lld, \"re_f\": %.17g, \"im_f\": %.17g}%s\n",
               i, re, im, table[i].real().to_double(), table[i].imag().to_double(),
               (i == EXT_LEN - 1) ? "" : ",");
    }
    printf("  ]\n}\n");
    return 0;
}
