// tb.cpp -- witness 2's check: one CG-like iteration (and the conjugate and broadcast forms) through
// vexec_top -- the RTL under co-simulation -- and through vexec_ref, the same code run as C. Any
// difference (a wrong DEPENDENCE pragma would make one) fails the run.
#include <cstdio>
#include <cstdlib>
#include "cfg.h"
#include "vexec.h"

typedef vx::Exec<vx::Cfg> E;

void vexec_top(hls::stream<vx::word_t>&, const E::lg_t[E::NQI][E::QD], E::lg_t[E::NQO][E::QD],
               hls::stream<vx::word_t>&);
void vexec_ref(hls::stream<vx::word_t>&, const E::lg_t[E::NQI][E::QD], E::lg_t[E::NQO][E::QD],
               hls::stream<vx::word_t>&);

static vx::word_t enc(int opc, int funct, int chain, int conj, int bcast, int rd, int rs1, int rs2,
                      int imm1, int imm2) {
    vx::word_t w = 0;
    w(6, 0) = opc;
    w(12, 7) = funct;
    w[13] = chain;
    w[14] = conj;
    w[15] = bcast;
    w(23, 16) = rd;
    w(31, 24) = rs1;
    w(39, 32) = rs2;
    w(51, 40) = imm1;
    w(63, 52) = imm2;
    return w;
}

static int program(vx::word_t* p) {
    using namespace vx;
    // groups: X 0, R 1, P 2, S 3, Y 5 (bank 1, used without R); scalars: ps 0, rz 1, al 2, be 3, rz2 5
    int n = 0;
    p[n++] = enc(OPC_C, C_SETVL, 0, 0, 0, 0, 0, 0, 8, 0);
    p[n++] = enc(OPC_V, V_MVI, 0, 0, 0, 0, 0, 0, 0, 0);                  // X = 0
    p[n++] = enc(OPC_Q, Q_VLSE, 1, 0, 0, 1, 0, 0, 0, 8);                 // R = q0[:, g 0]
    p[n++] = enc(OPC_V, V_MV, 1, 0, 0, 2, 1, 0, 0, 0);                   // P = R
    p[n++] = enc(OPC_V, V_DOTR, 0, 0, 0, 1, 1, 1, 0, 0);                 // rz = R.R
    p[n++] = enc(OPC_Q, Q_VLSE, 1, 0, 0, 3, 1, 0, 0, 8);                 // S = q1[:, g 0]
    p[n++] = enc(OPC_V, V_DOTR, 0, 0, 0, 0, 2, 3, 0, 0);                 // ps = P.S
    p[n++] = enc(OPC_S, S_DIVZ, 0, 0, 0, 2, 1, 0, 0, 0);                 // al = rz / ps
    p[n++] = enc(OPC_V, V_MACC_S, 1, 0, 0, 0, 2, 2, 0, 0);               // X += al P
    p[n++] = enc(OPC_V, V_NMSAC_S, 1, 0, 0, 1, 2, 3, 0, 0);              // R -= al S
    p[n++] = enc(OPC_V, V_DOTR, 0, 0, 0, 5, 1, 1, 0, 0);                 // rz2 = R.R (forwarded R)
    p[n++] = enc(OPC_S, S_DIVZ, 0, 0, 0, 3, 5, 1, 0, 0);                 // be = rz2 / rz
    p[n++] = enc(OPC_V, V_MADD_S, 1, 0, 0, 2, 3, 1, 0, 0);               // P = P be + R
    p[n++] = enc(OPC_Q, Q_VSSE, 0, 0, 0, 0, 2, 0, 0, 8);                 // q0out[:, g 0] = P
    p[n++] = enc(OPC_V, V_MUL, 1, 1, 1, 5, 0, 2, 0, 0);                  // Y = conj(X[0]) P (broadcast)
    p[n++] = enc(OPC_Q, Q_VSSE, 1, 0, 0, 1, 5, 0, 1, 8);                 // q1out[:, g 1] = Y
    p[n++] = enc(OPC_V, V_MACC, 0, 0, 0, 0, 1, 2, 0, 0);                 // X += R P
    p[n++] = enc(OPC_Q, Q_VSSE, 0, 0, 0, 1, 0, 0, 2, 8);                 // q1out[:, g 2] = X
    return n;
}

int main() {
    static E::lg_t qin[E::NQI][E::QD], out_rtl[E::NQO][E::QD], out_ref[E::NQO][E::QD];
    srand(7);
    for (int q = 0; q < E::NQI; ++q)
        for (int i = 0; i < E::QD; ++i)
            for (int b = 0; b < 2 * E::WQ * E::L; b += 16) qin[q][i](b + 15, b) = rand() & 0xffff;
    for (int q = 0; q < E::NQO; ++q)
        for (int i = 0; i < E::QD; ++i) out_rtl[q][i] = out_ref[q][i] = 0;
    vx::word_t prog[64];
    const int n = program(prog);
    int bad = 0;
    for (int job = 0; job < 2; ++job) {
        hls::stream<vx::word_t> p1, p2, s1, s2;
        vx::word_t hdr = 0;
        hdr(15, 0) = 100 + job;
        hdr(31, 16) = n;
        p1.write(hdr);
        p2.write(hdr);
        for (int k = 0; k < n; ++k) {
            p1.write(prog[k]);
            p2.write(prog[k]);
        }
        vexec_top(p1, qin, out_rtl, s1);
        vexec_ref(p2, qin, out_ref, s2);
        const vx::word_t a = s1.read(), b = s2.read();
        if (a != b) ++bad;
        printf("job %d: status rtl %s ref %s\n", job, a.to_string(16).c_str(), b.to_string(16).c_str());
    }
    long nonzero = 0;
    for (int q = 0; q < E::NQO; ++q)
        for (int i = 0; i < E::QD; ++i) {
            if (out_rtl[q][i] != out_ref[q][i]) ++bad;
            if (out_ref[q][i] != 0) ++nonzero;
        }
    unsigned long long sum = 0;
    for (int q = 0; q < E::NQO; ++q)
        for (int i = 0; i < E::QD; ++i)
            for (int b = 0; b < 2 * E::WQ * E::L; b += 32)
                sum = sum * 1000003ULL + (unsigned long long)out_ref[q][i](b + 31, b).to_uint64();
    printf("WITNESS-TB words compared %d, nonzero %ld, mismatches %d, checksum %016llx\n",
           E::NQO * E::QD, nonzero, bad, sum);
    return bad ? 1 : 0;
}
