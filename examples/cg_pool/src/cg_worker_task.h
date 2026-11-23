#ifndef EXAMPLES_CG_POOL_WORKER_TASK_H
#define EXAMPLES_CG_POOL_WORKER_TASK_H
// cg_worker_task.h -- one CG worker (HAND-WRITTEN), the HLS twin of examples/cg_pool/cg_pool.py::CgWorker,
// bit-exact with examples/cg_pool/cg_golden.py::fixed_solve.  ONE FIRING = ONE JOB:
//
//   1. the Job header (tx_id, m, c, t); out of range -> BAD_ARGS, fatal;
//   2. LOAD: the payload, a word a cycle -- H (N_rx x m) then B (N_rx x c), column by column -- into
//      the worker's own memory; R = q_R(B), D = q_P(R), X = 0, rz = q_rz(sum |R|^2) per column.
//      A payload of the wrong length -> BAD_LEN, fatal;
//   3. t iterations of CG in the factored form, S = D + H (H^H D), each a pipelined loop a pass:
//        MATMUL T  = q_T(H^H D) and S = q_S(D + H T), overlapped by a column: step s computes T's
//               column s and S's column s - 1, one H read feeding both banks of 16 complex MACs --
//               (c + 1) max(m, MIN_M) N_rx / 16 cycles (run 1 ran the two one after the other: 2 m c
//               N_rx / 16)
//        PS     ps = q_ps(sum Re(conj(D) S))    N_rx c / 4 cycles
//        ALPHA  alpha = q_alpha(w(rz) / ps)     c, the dividers pipelined; the stop rule
//        XR     X = q_X(X + D alpha), R = q_R(R - S alpha), rz' = q_rz(sum |R|^2)
//        BETA   beta = q_beta(w(rz') / rz)      c
//        DUPD   D = q_P(R + D beta)             (a stopped column keeps D, rz)
//   4. the answer: Resp(tx_id, N_rx c), X in q_Xo (N_rx x c, column by column), Status(tx_id, OK) --
//      three bursts, one frame for the pool's arbiter.
//
// Every right-hand side is exact (ap_fixed operators grow their result type, the accumulators hold a
// sum of every term), so the only roundings are the assignments to a register: the golden's quantize
// points (waveflow.linalg.cg's vec_step; the factored S of cg_golden.factored_s).
//
// THE STOP RULE: a column stops for good once rz <= CG_RZ_FLOOR at an iteration's start or ps <= 0;
// then alpha = 0 (X, R unchanged, exactly), and D and rz are kept.
//
// THE MEMORY: [word][column][lane] -- a word is 4 rows of a column.  H, D and S are split 16 ways
// (dim 1 cyclic 4 x the 4 lanes), so a matrix loop touches the 16 rows of a block in one cycle; R and
// X 4 ways (the lanes), a word a cycle.  Each bank is a constant index in every loop.
//
// AN ERROR IS FATAL: Resp(tx_id, 0), one zero word closed short, the error Status; then `halted` --
// cleared only by a reset -- and every later firing returns at once, reading nothing.
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "cg_pool_config.h"
#include "job.h"
#include "resp.h"
#include "status.h"

namespace cg_worker_body {

using namespace cg_cfg;
static const int L = CG_LANES;              // samples a word, and the vector loops' lanes
static const int G = 4;                     // words a 16-row block: P = G * L complex MACs a cycle
static const int NWD = CG_COLW;             // words a column
static const int NB = CG_COLW / G;          // 16-row blocks a column
static const int MIN_M = CG_MIN_M;          // the fewest columns of H a MATMUL step walks (T's hazard)

template <typename TO>
static void fail(hls::stream<TO>& m_out, ap_uint<16> tx, StatusCode code) {
    Resp r;
    r.tx_id = tx;
    r.n = 0;
    streamutils::write_msg<CG_W>(r, m_out);
    streamutils::write_boundary_word<TO, CG_W>(m_out, ap_uint<CG_W>(0), true);
    Status st;
    st.tx_id = tx;
    st.status = code;
    streamutils::write_msg<CG_W>(st, m_out);
}

}  // namespace cg_worker_body

// K is the worker's index in its pool: each worker its own instantiation, so its own `halted` (one
// static shared by two tasks is a feedback dependence csynth refuses).
template <int K, typename TI, typename TO>
static void cg_worker_task(hls::stream<TI>& s_in, hls::stream<TO>& m_out) {
    using namespace cg_cfg;
    using namespace cg_worker_body;
    typedef typename cg_h::value_type hin_t;     // a sample of H or B on s_in
    typedef typename cg_x::value_type xout_t;    // a sample of X on m_out

    static bool halted = false;
#pragma HLS reset variable=halted
    if (halted) return;

    // 1. the job
    Job job;
    streamutils::read_msg<CG_W>(job, s_in);
    const ap_uint<16> tx = job.tx_id;
    const int m = job.m, c = job.c, t = job.t;
    if (m < 1 || m > CG_MMAX || c < 1 || c > CG_CMAX || t < 1 || t > CG_TMAX) {
        fail(m_out, tx, StatusCode::BAD_ARGS);
        halted = true;
        return;
    }

    h_t hr[NWD][CG_MMAX][L], hi[NWD][CG_MMAX][L];
#pragma HLS ARRAY_PARTITION variable=hr dim=1 type=cyclic factor=4
#pragma HLS ARRAY_PARTITION variable=hr dim=3 type=complete
#pragma HLS ARRAY_PARTITION variable=hi dim=1 type=cyclic factor=4
#pragma HLS ARRAY_PARTITION variable=hi dim=3 type=complete
    p_t dr[NWD][CG_CMAX][L], di[NWD][CG_CMAX][L];
#pragma HLS ARRAY_PARTITION variable=dr dim=1 type=cyclic factor=4
#pragma HLS ARRAY_PARTITION variable=dr dim=3 type=complete
#pragma HLS ARRAY_PARTITION variable=di dim=1 type=cyclic factor=4
#pragma HLS ARRAY_PARTITION variable=di dim=3 type=complete
    s_t sr[NWD][CG_CMAX][L], si[NWD][CG_CMAX][L];
#pragma HLS ARRAY_PARTITION variable=sr dim=1 type=cyclic factor=4
#pragma HLS ARRAY_PARTITION variable=sr dim=3 type=complete
#pragma HLS ARRAY_PARTITION variable=si dim=1 type=cyclic factor=4
#pragma HLS ARRAY_PARTITION variable=si dim=3 type=complete
    r_t rr[NWD][CG_CMAX][L], ri[NWD][CG_CMAX][L];
#pragma HLS ARRAY_PARTITION variable=rr dim=3 type=complete
#pragma HLS ARRAY_PARTITION variable=ri dim=3 type=complete
    x_t xr[NWD][CG_CMAX][L], xi[NWD][CG_CMAX][L];
#pragma HLS ARRAY_PARTITION variable=xr dim=3 type=complete
#pragma HLS ARRAY_PARTITION variable=xi dim=3 type=complete
    t_t tr[CG_MMAX][CG_CMAX], ti[CG_MMAX][CG_CMAX];
    rz_t rz[CG_CMAX], rz2[CG_CMAX];
    ps_t ps[CG_CMAX];
    alpha_t al[CG_CMAX];
    bool stop[CG_CMAX];

    // 2. the payload: H then B, a word a cycle.  rz is summed over each B column's 16 words.
    const int nh = NWD * m;
    const int nw = NWD * (m + c);
    bool short_burst = false, long_burst = false;
    dot_rz_t racc = 0;
    int wi = 0, col = 0;                          // word within the column, and the column
LOAD:
    for (int w = 0; w < nw; ++w) {
#pragma HLS PIPELINE II=1
        hin_t v[L];
#pragma HLS ARRAY_PARTITION variable=v complete dim=1
        streamutils::tlast_status ts = streamutils::tlast_status::no_tlast;
        cg_h::read_boundary_lane<CG_W>(s_in, v, L, ts);
        const bool last = (ts != streamutils::tlast_status::no_tlast);
        if (w < nh) {
            for (int q = 0; q < L; ++q) {
#pragma HLS UNROLL
                hr[wi][col][q] = v[q].real();
                hi[wi][col][q] = v[q].imag();
            }
        } else {
            dot_rz_t s = 0;
            for (int q = 0; q < L; ++q) {
#pragma HLS UNROLL
                const r_t a = v[q].real(), b = v[q].imag();
                rr[wi][col][q] = a;
                ri[wi][col][q] = b;
                dr[wi][col][q] = a;
                di[wi][col][q] = b;
                xr[wi][col][q] = 0;
                xi[wi][col][q] = 0;
                s += a * a + b * b;
            }
            racc += s;
            if (wi == NWD - 1) {
                rz[col] = racc;
                stop[col] = false;
                racc = 0;
            }
        }
        if (wi == NWD - 1) {
            wi = 0;
            col = (w == nh - 1) ? 0 : col + 1;
        } else {
            ++wi;
        }
        if (last && w < nw - 1) {
            short_burst = true;
            break;
        }
        if (w == nw - 1 && !last) long_burst = true;
    }
    if (short_burst || long_burst) {
        fail(m_out, tx, StatusCode::BAD_LEN);
        halted = true;
        return;
    }

    // 3. t iterations
ITER:
    for (int it = 0; it < t; ++it) {
        for (int k = 0; k < c; ++k) {
#pragma HLS PIPELINE II=1
            if (rz[k] <= rz_t(CG_RZ_FLOOR_VAL)) stop[k] = true;
        }

        // MATMUL: TMUL and UMUL fused, overlapped by a column.  Column step s runs TMUL on column s
        // (T[j][s] = q_T(sum_n conj(H[n][j]) D[n][s])) and UMUL on column s - 1 (S[n][s-1] =
        // q_S(D[n][s-1] + sum_j H[n][j] T[j][s-1])), both walking H in one order -- column j, its 16-row
        // blocks b -- so one read of H feeds both banks of 16 complex MACs.  c + 1 steps, not 2c.
        //   T: TMUL writes column s while UMUL reads column s - 1 (one write, one read a cycle).  The
        //   value read was written a step before: mm * NB - 3 iterations, more than the pipeline's depth
        //   only if a step is long enough -- so a step walks at least MIN_M columns of H (mm), the ones
        //   past m adding zero (E0's smallest set is m = 8: no cost there).
        //   UMUL keeps a running sum for every row of its S column (NB blocks x 16), written at the
        //   step's end.
        // The counters (s, j, b) decide nothing about the data: they only say which column each bank
        // works on.
        const int mm = (m < MIN_M) ? MIN_M : m;
        acc_t_t tar = 0, tai = 0;
        acc_u_t ur[NB][G][L], ui[NB][G][L];
#pragma HLS ARRAY_PARTITION variable=ur complete dim=0
#pragma HLS ARRAY_PARTITION variable=ui complete dim=0
        int j = 0, k = 0, b = 0;                  // k: the step s -- TMUL's column; UMUL's is k - 1
    MATMUL:
        for (int i = 0; i < (c + 1) * mm * NB; ++i) {
#pragma HLS PIPELINE II=1
// T's column k is written in step k and read in step k + 1, at least MIN_M * NB - 3 iterations later.
#pragma HLS DEPENDENCE variable=tr type=inter false
#pragma HLS DEPENDENCE variable=ti type=inter false
            const bool live = (j < m);                    // a column of H, not the step's padding
            const bool do_t = live && (k < c);
            const int kt = (k < c) ? k : 0;               // TMUL's column (any, when it is idle)
            const int ku = (k > 0) ? k - 1 : 0;           // UMUL's column (any, when it is idle)
            const bool do_u = (k > 0);
            const t_t x = live ? tr[j][ku] : t_t(0);      // the step's padding adds zero
            const t_t y = live ? ti[j][ku] : t_t(0);
            acc_t_t pr = 0, pi = 0;
            for (int g = 0; g < G; ++g) {
#pragma HLS UNROLL
                for (int q = 0; q < L; ++q) {
#pragma HLS UNROLL
                    const h_t a = hr[b * G + g][j][q], bb = hi[b * G + g][j][q];
                    // TMUL, column kt
                    const p_t dx = dr[b * G + g][kt][q], dy = di[b * G + g][kt][q];
                    pr += a * dx + bb * dy;
                    pi += a * dy - bb * dx;
                    // UMUL, column ku
                    const acc_u_t ur0 = (j == 0) ? acc_u_t(0) : ur[b][g][q];
                    const acc_u_t ui0 = (j == 0) ? acc_u_t(0) : ui[b][g][q];
                    const acc_u_t nr = ur0 + (a * x - bb * y);
                    const acc_u_t ni = ui0 + (a * y + bb * x);
                    ur[b][g][q] = nr;
                    ui[b][g][q] = ni;
                    if (do_u && j == mm - 1) {
                        sr[b * G + g][ku][q] = dr[b * G + g][ku][q] + nr;
                        si[b * G + g][ku][q] = di[b * G + g][ku][q] + ni;
                    }
                }
            }
            const acc_t_t nr = (b == 0) ? pr : acc_t_t(tar + pr);
            const acc_t_t ni = (b == 0) ? pi : acc_t_t(tai + pi);
            tar = nr;
            tai = ni;
            if (do_t && b == NB - 1) {
                tr[j][kt] = nr;
                ti[j][kt] = ni;
            }
            if (b == NB - 1) {
                b = 0;
                if (j == mm - 1) {
                    j = 0;
                    ++k;
                } else {
                    ++j;
                }
            } else {
                ++b;
            }
        }

        // PS: ps[k] = q_ps(sum Re(conj(D) S)), a word a cycle
        dot_ps_t pacc = 0;
        int wd = 0;
        k = 0;
    PS:
        for (int i = 0; i < c * NWD; ++i) {
#pragma HLS PIPELINE II=1
            dot_ps_t s = 0;
            for (int q = 0; q < L; ++q) {
#pragma HLS UNROLL
                s += dr[wd][k][q] * sr[wd][k][q] + di[wd][k][q] * si[wd][k][q];
            }
            const dot_ps_t n = (wd == 0) ? s : dot_ps_t(pacc + s);
            pacc = n;
            if (wd == NWD - 1) {
                ps[k] = n;
                wd = 0;
                ++k;
            } else {
                ++wd;
            }
        }

        // ALPHA: alpha = q_alpha(w(rz) / ps) -- the divisor made safe, then selected, so the dividers
        // pipeline; the stop rule's second test.  A stopped column's alpha is 0.
    ALPHA:
        for (int kk = 0; kk < c; ++kk) {
#pragma HLS PIPELINE II=1
            const ps_t p = ps[kk];
            const bool st = stop[kk] || (p <= 0);
            const ps_t den = (p == 0) ? ps_t(1) : p;
            const rzw_t num = rz[kk];
            const alpha_t q = num / den;
            al[kk] = st ? alpha_t(0) : q;
            stop[kk] = st;
        }

        // XR: X = q_X(X + D alpha), R = q_R(R - S alpha), rz' = q_rz(sum |R|^2)
        dot_rz_t zacc = 0;
        wd = 0;
        k = 0;
    XR:
        for (int i = 0; i < c * NWD; ++i) {
#pragma HLS PIPELINE II=1
// Each iteration reads and rewrites its own word: no word is touched by two iterations (csynth assumed
// one was, distance 2, and halved the loop's rate).
#pragma HLS DEPENDENCE variable=rr type=inter false
#pragma HLS DEPENDENCE variable=ri type=inter false
#pragma HLS DEPENDENCE variable=xr type=inter false
#pragma HLS DEPENDENCE variable=xi type=inter false
            const alpha_t a = al[k];
            dot_rz_t s = 0;
            for (int q = 0; q < L; ++q) {
#pragma HLS UNROLL
                xr[wd][k][q] = xr[wd][k][q] + dr[wd][k][q] * a;
                xi[wd][k][q] = xi[wd][k][q] + di[wd][k][q] * a;
                const r_t nr = rr[wd][k][q] - sr[wd][k][q] * a;
                const r_t ni = ri[wd][k][q] - si[wd][k][q] * a;
                rr[wd][k][q] = nr;
                ri[wd][k][q] = ni;
                s += nr * nr + ni * ni;
            }
            const dot_rz_t n = (wd == 0) ? s : dot_rz_t(zacc + s);
            zacc = n;
            if (wd == NWD - 1) {
                rz2[k] = n;
                wd = 0;
                ++k;
            } else {
                ++wd;
            }
        }

        // BETA: beta = q_beta(w(rz') / rz), 0 if rz == 0; rz = rz' (a stopped column keeps both)
        beta_t be[CG_CMAX];
    BETA:
        for (int kk = 0; kk < c; ++kk) {
#pragma HLS PIPELINE II=1
            const rz_t r0 = rz[kk];
            const rz_t den = (r0 == 0) ? rz_t(1) : r0;
            const rzw_t num = rz2[kk];
            const beta_t q = num / den;
            be[kk] = (r0 == 0) ? beta_t(0) : q;
            if (!stop[kk]) rz[kk] = rz2[kk];
        }

        // DUPD: D = q_P(R + D beta), a word a cycle; a stopped column keeps D
        wd = 0;
        k = 0;
    DUPD:
        for (int i = 0; i < c * NWD; ++i) {
#pragma HLS PIPELINE II=1
#pragma HLS DEPENDENCE variable=dr type=inter false
#pragma HLS DEPENDENCE variable=di type=inter false
            const beta_t bt = be[k];
            const bool keep = stop[k];
            for (int q = 0; q < L; ++q) {
#pragma HLS UNROLL
                if (!keep) {
                    dr[wd][k][q] = rr[wd][k][q] + dr[wd][k][q] * bt;
                    di[wd][k][q] = ri[wd][k][q] + di[wd][k][q] * bt;
                }
            }
            if (wd == NWD - 1) {
                wd = 0;
                ++k;
            } else {
                ++wd;
            }
        }
    }

    // 4. the answer
    Resp r;
    r.tx_id = tx;
    r.n = CG_NRX * c;
    streamutils::write_msg<CG_W>(r, m_out);
    int wd = 0, k = 0;
STORE:
    for (int i = 0; i < c * NWD; ++i) {
#pragma HLS PIPELINE II=1
        xout_t v[L];
#pragma HLS ARRAY_PARTITION variable=v complete dim=1
        for (int q = 0; q < L; ++q) {
#pragma HLS UNROLL
            const xo_t a = xr[wd][k][q], b = xi[wd][k][q];
            v[q] = xout_t(a, b);
        }
        cg_x::write_boundary_lane<CG_W>(v, m_out, i == c * NWD - 1, L);
        if (wd == NWD - 1) {
            wd = 0;
            ++k;
        } else {
            ++wd;
        }
    }
    Status st;
    st.tx_id = tx;
    st.status = StatusCode::OK;
    streamutils::write_msg<CG_W>(st, m_out);
}

#endif  // EXAMPLES_CG_POOL_WORKER_TASK_H
