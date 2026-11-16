// w1.cpp -- witness 1: one cplx4 lane's datapath alone, pipelined at II 1, an op per element (the op
// changes every cycle: the general case).  Its loop depth is the lane's D.  SC_E32MUL adds the e32
// real multiply on the same four multipliers.
#include "lane.h"

#define N 256

void w1_top(const ap_uint<5> op[N], const word_t a[N], const word_t b[N], const word_t c[N],
            const ap_uint<2> m[N], word_t x, ap_uint<5> csh, ap_uint<5> rsh, word_t r[N],
            ap_uint<2> mo[N]) {
LANE:
    for (int i = 0; i < N; i++) {
#pragma HLS pipeline II = 1
        ap_uint<2> o;
        r[i] = lane_op(op[i], a[i], b[i], c[i], x, csh, rsh, m[i], o);
        mo[i] = o;
    }
}
