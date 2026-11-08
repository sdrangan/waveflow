// w6.cpp -- witness 6 (and 2): the flattened instruction x element loop (plans/simdcore.md, Stage 1).
//
// One pipelined loop over (instruction, element group t): each iteration runs element group t of the
// current instruction on all SC_L lanes (element e = t*SC_L + lane), then advances t, and at t == ng
// the next instruction.  The vector register file is per lane, 32 registers x T_MAX words, held in
// three copies (one per read operand: vs2, vs1, vd), each a simple dual-port memory written alike, so
// a lane reads three words and writes one a cycle.  With `dependence ... inter false` HLS does not
// stall a read behind an earlier write; that is correct only when ng >= the loop's depth (VL/L >= D).
//
// Flags: SC_L lanes (2); SC_NODEP drops the dependence pragmas (HLS's default behaviour);
// SC_E32MUL adds the e32 real multiply (lane.h).
#include "lane.h"

#ifndef SC_L
#define SC_L 2
#endif
#define VLE 64            // e32 elements a register (VLEN 2048)
#define NREG 32
#define T_MAX (VLE / SC_L)
#define MAXI 64

// instruction word: op[4:0] vd[9:5] vs1[14:10] vs2[19:15] csh[24:20] rsh[29:25] ng-1[31:26]... see tb
// ng is 6 bits [31:26] holding ng-1 (1..64 groups); x [63:32].
typedef ap_uint<64> iword_t;

void w6_top(const iword_t prog[MAXI], int nins, int total, const word_t rin[NREG * VLE],
            word_t rout[NREG * VLE]) {
    word_t rfa[SC_L][NREG * T_MAX], rfb[SC_L][NREG * T_MAX], rfc[SC_L][NREG * T_MAX];
    ap_uint<2> mrf[SC_L][T_MAX];
#pragma HLS array_partition variable = rfa dim = 1 complete
#pragma HLS array_partition variable = rfb dim = 1 complete
#pragma HLS array_partition variable = rfc dim = 1 complete
#pragma HLS array_partition variable = mrf dim = 1 complete
#pragma HLS bind_storage variable = rfa type = ram_s2p impl = bram
#pragma HLS bind_storage variable = rfb type = ram_s2p impl = bram
#pragma HLS bind_storage variable = rfc type = ram_s2p impl = bram
    iword_t pm[MAXI];

LOAD_P:
    for (int i = 0; i < MAXI; i++) {
#pragma HLS pipeline II = 1
        pm[i] = (i < nins) ? prog[i] : iword_t(0);
    }
LOAD_R:
    for (int i = 0; i < NREG * VLE; i++) {
#pragma HLS pipeline II = 1
        int r = i / VLE, e = i % VLE, ln = e % SC_L, t = e / SC_L;
        word_t w = rin[i];
        for (int l = 0; l < SC_L; l++) {
            if (l == ln) {
                rfa[l][r * T_MAX + t] = w;
                rfb[l][r * T_MAX + t] = w;
                rfc[l][r * T_MAX + t] = w;
            }
        }
        if (r == 0) {
            for (int l = 0; l < SC_L; l++)
                if (l == ln) mrf[l][t] = 0;
        }
    }

    int pc = 0;
    ap_uint<7> t = 0;
    iword_t in = pm[0];
EXEC:
    for (int it = 0; it < total; it++) {
#pragma HLS pipeline II = 1
#pragma HLS loop_tripcount min = 64 max = 2048
#ifndef SC_NODEP
#pragma HLS dependence variable = rfa inter false
#pragma HLS dependence variable = rfb inter false
#pragma HLS dependence variable = rfc inter false
#pragma HLS dependence variable = mrf inter false
#endif
        ap_uint<5> op = in.range(4, 0), vd = in.range(9, 5), vs1 = in.range(14, 10),
                   vs2 = in.range(19, 15), csh = in.range(24, 20), rsh = in.range(29, 25);
        ap_uint<7> ng = ap_uint<7>(in.range(31, 26)) + 1;
        word_t x = in.range(63, 32);
        for (int l = 0; l < SC_L; l++) {
#pragma HLS unroll
            word_t a = rfa[l][vs2 * T_MAX + t];
            word_t b = rfb[l][vs1 * T_MAX + t];
            word_t c = rfc[l][vd * T_MAX + t];
            ap_uint<2> mo;
            word_t r = lane_op(op, a, b, c, x, csh, rsh, mrf[l][t], mo);
            if (op == MSLT16) mrf[l][t] = mo;
            rfa[l][vd * T_MAX + t] = r;
            rfb[l][vd * T_MAX + t] = r;
            rfc[l][vd * T_MAX + t] = r;
        }
        if (t == ng - 1) {
            t = 0;
            pc = (pc + 1) % MAXI;
            in = pm[pc];
        } else {
            t++;
        }
    }

DUMP:
    for (int i = 0; i < NREG * VLE; i++) {
#pragma HLS pipeline II = 1
        int r = i / VLE, e = i % VLE, ln = e % SC_L, tt = e / SC_L;
        word_t w = 0;
        for (int l = 0; l < SC_L; l++)
            if (l == ln) w = rfa[l][r * T_MAX + tt];
        rout[i] = w;
    }
}
