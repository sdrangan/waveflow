// w4.cpp -- witness 4 (resources only): an input window's ping-pong copies (records D10).  Per cycle
// the loader writes one 64-bit stream word -- two complex samples (i, j) and (i, j+1) of a k x n block,
// row-major -- into the NEXT copy, column-major (register j holds column j: element i in lane i % L,
// group i / L), while the program reads two operands of the CURRENT copy.  Two copies, each a
// true dual-port memory a lane (the loader's two writes), selected by the copy bit.
#include <hls_stream.h>
#include "lane.h"

#ifndef SC_L
#define SC_L 2
#endif
#define VLE 64
#define T_MAX (VLE / SC_L)
#define NW 8  // registers in the window (n antennas)

void w4_top(hls::stream<ap_uint<64>> &in, ap_uint<1> cur, const ap_uint<5> ra[VLE * NW / SC_L],
            const ap_uint<5> rb[VLE * NW / SC_L], word_t out[SC_L][VLE * NW / SC_L]) {
#pragma HLS array_partition variable = out dim = 1 complete
    static word_t win[2][SC_L][NW * T_MAX];
#pragma HLS array_partition variable = win dim = 1 complete
#pragma HLS array_partition variable = win dim = 2 complete
#pragma HLS bind_storage variable = win type = ram_t2p impl = bram
    // k = 64 rows, n = 8 columns: 256 words, one a cycle; the program reads 256 groups meanwhile
STEP:
    for (int w = 0; w < VLE * NW / 2; w++) {
#pragma HLS pipeline II = 1
#pragma HLS dependence variable = win inter false
        ap_uint<64> d = in.read();
        int i = w / (NW / 2), j = 2 * (w % (NW / 2));
        int ln = i % SC_L, t = i / SC_L;
        // per copy and lane, the two ports serve the loader (the next copy) or the program (the
        // current one): exclusive branches, so two ports suffice
        int g = w % (VLE * NW / SC_L), t2 = g % T_MAX;
        int pa = (ra[g] % NW) * T_MAX + t2, pb = (rb[g] % NW) * T_MAX + t2;
        int wa = j * T_MAX + t, wb = (j + 1) * T_MAX + t;
        for (int l = 0; l < SC_L; l++) {
#pragma HLS unroll
            word_t va = 0, vb = 0;
            for (int c = 0; c < 2; c++) {
#pragma HLS unroll
                if (c != cur) {
                    if (l == ln) {
                        win[c][l][wa] = d.range(31, 0);
                        win[c][l][wb] = d.range(63, 32);
                    }
                } else {
                    va = win[c][l][pa];
                    vb = win[c][l][pb];
                }
            }
            out[l][g] = va ^ vb;
        }
    }
}
