// w3.cpp -- witness 3: the reduction (vredsum / vredmax at e32).  Each lane reduces its own elements
// (element e = t*SC_L + lane) in a pipelined loop, then one log2(SC_L) tree combines the lanes and the
// scalar init (vs1[0]).
#include "lane.h"

#ifndef SC_L
#define SC_L 2
#endif
#define VLE 64
#define T_MAX (VLE / SC_L)

void w3_top(const word_t v[SC_L][T_MAX], int ng, bool is_max, word_t init, word_t *out) {
#pragma HLS array_partition variable = v dim = 1 complete
    ap_int<32> acc[SC_L];
#pragma HLS array_partition variable = acc complete
    for (int l = 0; l < SC_L; l++) {
#pragma HLS unroll
        acc[l] = is_max ? ap_int<32>(-2147483647 - 1) : ap_int<32>(0);
    }
ACC:
    for (int t = 0; t < ng; t++) {
#pragma HLS pipeline II = 1
#pragma HLS loop_tripcount min = 1 max = T_MAX
        for (int l = 0; l < SC_L; l++) {
#pragma HLS unroll
            ap_int<32> x = v[l][t];
            acc[l] = is_max ? (x > acc[l] ? x : acc[l]) : ap_int<32>(acc[l] + x);
        }
    }
    // the tree
    for (int w = SC_L / 2; w >= 1; w /= 2) {
#pragma HLS unroll
        for (int l = 0; l < w; l++) {
#pragma HLS unroll
            ap_int<32> p = acc[l], q = acc[l + w];
            acc[l] = is_max ? (p > q ? p : q) : ap_int<32>(p + q);
        }
    }
    ap_int<32> s = init;
    *out = is_max ? (acc[0] > s ? acc[0] : s) : ap_int<32>(acc[0] + s);
}
