// tb6.cpp -- testbench of w6_top: a chain of dependent instructions, every register word compared with
// a sequential C reference (lane_op element by element, instruction by instruction).
// SC_NG sets the groups an instruction (default T_MAX: VL = 64); a small SC_NG (VL/L < D) is the
// hazard case, expected to FAIL at RTL when the dependence pragma is on.
#include <cstdio>
#include <cstdlib>
#include "lane.h"

#ifndef SC_L
#define SC_L 2
#endif
#define VLE 64
#define NREG 32
#define T_MAX (VLE / SC_L)
#define MAXI 64
#ifndef SC_NG
#define SC_NG T_MAX
#endif

typedef ap_uint<64> iword_t;
void w6_top(const iword_t prog[MAXI], int nins, int total, const word_t rin[NREG * VLE],
            word_t rout[NREG * VLE]);

static iword_t enc(int op, int vd, int vs2, int vs1, int csh, int rsh, int ng, unsigned x) {
    iword_t w = 0;
    w.range(4, 0) = op;
    w.range(9, 5) = vd;
    w.range(14, 10) = vs1;
    w.range(19, 15) = vs2;
    w.range(24, 20) = csh;
    w.range(29, 25) = rsh;
    w.range(31, 26) = ng - 1;
    w.range(63, 32) = x;
    return w;
}

int main() {
    static word_t rin[NREG * VLE], rout[NREG * VLE], ref[NREG * VLE];
    static ap_uint<2> mref[VLE];
    srand(7);
    for (int i = 0; i < NREG * VLE; i++) {
        unsigned v = ((unsigned)rand() << 16) ^ (unsigned)rand();
        rin[i] = v;
        ref[i] = v;
    }
    for (int e = 0; e < VLE; e++) mref[e] = 0;

    const int ng = SC_NG;
    // a dependent chain: each instruction reads the previous one's destination
    iword_t prog[MAXI];
    int n = 0;
    prog[n++] = enc(CMUL, 3, 1, 2, 15, 0, ng, 0);      // v3 = v1 * v2
    prog[n++] = enc(CMULC, 4, 3, 1, 14, 0, ng, 0);     // v4 = conj(v3) * v1
    prog[n++] = enc(CMACC, 4, 3, 2, 15, 0, ng, 0);     // v4 += v3 * v2
    prog[n++] = enc(CDOTR, 5, 4, 3, 0, 6, ng, 0);      // v5 = Re(conj(v4) v3) >> 6
    prog[n++] = enc(ADD16, 6, 5, 4, 0, 0, ng, 0);      // v6 = v5 + v4 (e16)
    prog[n++] = enc(MAX16, 7, 6, 1, 0, 0, ng, 0);      // v7 = max(v6, v1)
    prog[n++] = enc(MSLT16, 8, 7, 2, 0, 0, ng, 0);     // mask = v7 < v2
    prog[n++] = enc(MERGE16, 9, 7, 2, 0, 0, ng, 0);    // v9 = mask ? v2 : v7
    prog[n++] = enc(SMUL16, 10, 9, 6, 0, 0, ng, 0);    // v10 = v9 * v6 (Q15)
    prog[n++] = enc(SSRA16, 11, 10, 0, 0, 0, ng, 3);   // v11 = v10 >> 3 (rnu)
    prog[n++] = enc(SUB32, 12, 11, 5, 0, 0, ng, 0);    // v12 = v11 - v5 (e32)
    prog[n++] = enc(CMUL, 13, 12, 11, 15, 0, ng, 0);   // v13 = v12 * v11
    for (int i = n; i < MAXI; i++) prog[i] = 0;
    int total = n * ng;

    // reference
    for (int k = 0; k < n; k++) {
        iword_t in = prog[k];
        int op = in.range(4, 0), vd = in.range(9, 5), vs1 = in.range(14, 10), vs2 = in.range(19, 15);
        ap_uint<5> csh = in.range(24, 20), rsh = in.range(29, 25);
        word_t x = in.range(63, 32);
        for (int e = 0; e < ng * SC_L; e++) {
            ap_uint<2> mo;
            word_t r = lane_op(op, ref[vs2 * VLE + e], ref[vs1 * VLE + e], ref[vd * VLE + e], x, csh, rsh,
                               mref[e], mo);
            if (op == MSLT16) mref[e] = mo;
            ref[vd * VLE + e] = r;
        }
    }

    w6_top(prog, n, total, rin, rout);

    int bad = 0;
    for (int i = 0; i < NREG * VLE; i++) {
        if (rout[i] != ref[i]) {
            if (bad < 8) printf("MISMATCH v%d[%d]: got %08x want %08x\n", i / VLE, i % VLE,
                                (unsigned)rout[i], (unsigned)ref[i]);
            bad++;
        }
    }
    printf("w6: L=%d ng=%d instructions=%d total=%d mismatches=%d -> %s\n", SC_L, ng, n, total, bad,
           bad ? "FAIL" : "PASS");
    return bad ? 1 : 0;
}
