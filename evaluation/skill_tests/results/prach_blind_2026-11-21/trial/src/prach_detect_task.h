#ifndef PRACH_DETECT_TASK_H
#define PRACH_DETECT_TASK_H
// prach_detect_task.h -- the searcher's last stage (HAND-WRITTEN), the HLS twin of
// prach/prach.py::Detect.run_iter and prach/golden.py::power / detect_root.  ONE FIRING = ONE COMMAND:
//
//   1. read CorHdr;
//   2. when OK, per root: n_ant frames of 64 words from fft256 (ap_fixed<25,11>), a word a cycle:
//      q = z * 2^8 assigned to ap_fixed<18,18,AP_RND,AP_SAT> (golden's sat18(rnd(z_stored / 2^6))),
//      P[n] (+)= qr^2 + qi^2 (exact integers);
//      then per cyclic shift v with preamble r per_root + v < n_pre: its window (golden.window: the
//      zero-delay point less WIN_MARGIN = 4), the first maximum in window order, and the noise mask
//      cleared +-NOISE_EXCL = 2 round it; the noise S (the profile where the mask is set) and its
//      count N; per window the tests peak N 2^8 > thr_q8 S and 40 peak > the root's strongest peak
//      (golden.detect_root, decision 17) -- a DetRec kept when both pass;
//   3. Resp(tx_id, n_det, exp), the n_det records as one burst (none when 0), Status: the last
//      stage answers, errors included; on an error, halt.
//
// NOT COMPILED HERE: Vitis HLS is not installed on the machine this was written on.
#include "hls_stream.h"
#include <ap_int.h>
#include <ap_fixed.h>
#include "streamutils_hls.h"
#include "complex__fixed25_11_array_utils.h"
#include "status_code.h"
#include "cor_hdr.h"
#include "resp.h"
#include "det_rec.h"
#include "status.h"

#ifndef PRACH_WIN_MARGIN
#define PRACH_WIN_MARGIN 4                             // golden.WIN_MARGIN
#endif
#ifndef PRACH_NOISE_EXCL
#define PRACH_NOISE_EXCL 2                             // golden.NOISE_EXCL
#endif
// golden.REL_Q = 40: a peak is reported only if 40 x peak > the root's strongest window peak

template <int W, typename TC, typename TO, typename TS>
static void prach_detect_task(hls::stream<TC>& s_cmd, hls::stream<ap_uint<200> >& s_fft,
                              hls::stream<TO>& m_out, hls::stream<TS>& m_status) {
    static_assert(W == 128, "prach_detect_task: one record a word");
    static bool halted = false;
#pragma HLS reset variable=halted
    if (halted) {
        return;
    }
    namespace xz = complex__fixed25_11_array_utils;
    typedef ap_fixed<18, 18, AP_RND, AP_SAT> q_t;      // sat18(rnd(z / 2^6)) in stored units
    typedef ap_uint<38> p_t;                           // sum over <= 4 antennas of |q|^2 < 2^36
    typedef ap_uint<46> s_t;                           // the profile's sum over 256 points

    // 1. the command
    CorHdr h;
    streamutils::read_msg<W>(h, s_cmd);
    const int n_ant = h.n_ant;
    const int n_cs = h.n_cs;
    const int n_pre = h.n_pre;
    const int n_roots = (h.code == StatusCode::OK) ? int(h.n_roots) : 0;
    const int per_root = (n_cs == 0) ? 1 : 139 / n_cs;
    const ap_uint<16> thr = h.thr_q8;

    DetRec dets[64];
    int n_det = 0;
    p_t pdp[4][64];                                    // bank m = n mod 4, row n / 4
#pragma HLS ARRAY_PARTITION variable=pdp complete dim=1
    ap_uint<1> keep[4][64];                            // the noise mask, banked as pdp
#pragma HLS ARRAY_PARTITION variable=keep complete dim=1

ROOT:
    for (int r = 0; r < n_roots; ++r) {
        // 2a. the profile: n_ant frames, a word a cycle
        int a = 0, t = 0;
    PDP:
        for (int i = 0; i < n_ant * 64; ++i) {
#pragma HLS PIPELINE II=1
#pragma HLS DEPENDENCE variable=pdp inter false
            xz::value_type z[4];
#pragma HLS ARRAY_PARTITION variable=z complete dim=1
            xz::read_stream_lane<200>(s_fft, z);
            for (int m = 0; m < 4; ++m) {
#pragma HLS UNROLL
                const q_t qr = z[m].real() * ap_uint<9>(256);
                const q_t qi = z[m].imag() * ap_uint<9>(256);
                const ap_int<18> ir = qr;
                const ap_int<18> ii = qi;
                const p_t pw = p_t(ir * ir) + p_t(ii * ii);
                const p_t acc = (a == 0) ? pw : p_t(pdp[m][t] + pw);
                pdp[m][t] = acc;
            }
            if (++t == 64) {
                t = 0;
                ++a;
            }
        }
        // 2b. the windows of this root's shifts: each one's peak; the noise mask cleared round it
        p_t wbest[64];
        ap_uint<8> wn[64], wj[64];
        int nwin = 0;
        p_t pmax = 0;
    MASK_INIT:
        for (int t2 = 0; t2 < 64; ++t2) {
#pragma HLS PIPELINE II=1
            for (int m = 0; m < 4; ++m) {
#pragma HLS UNROLL
                keep[m][t2] = 1;
            }
        }
    SHIFT:
        for (int v = 0; v < per_root; ++v) {
            const int pre = r * per_root + v;
            if (pre >= n_pre) {
                break;
            }
            int n0 = 0, len = 256;
            if (n_cs != 0) {
                const int cv = v * n_cs;
                const int aa = (139 - cv) % 139;
                const int ns = (aa * 256 + 138) / 139;
                const int ne = ((aa + n_cs) * 256 + 138) / 139;
                n0 = (ns - PRACH_WIN_MARGIN) & 255;
                len = ne - ns;
            }
            p_t best = 0;
            int jbest = 0;
        WIN:
            for (int j = 0; j < len; ++j) {
#pragma HLS PIPELINE II=1
                const ap_uint<8> n = n0 + j;               // mod 256
                const p_t val = pdp[n & 3][n >> 2];
                if (j == 0 || val > best) {                // the first maximum
                    best = val;
                    jbest = j;
                }
            }
            const ap_uint<8> npk = n0 + jbest;             // mod 256
            wbest[nwin] = best;
            wn[nwin] = npk;
            wj[nwin] = jbest;
            ++nwin;
            if (best > pmax) {
                pmax = best;
            }
        EXCL:
            for (int d = -PRACH_NOISE_EXCL; d <= PRACH_NOISE_EXCL; ++d) {
#pragma HLS PIPELINE II=1
                const ap_uint<8> n = npk + d;              // mod 256
                keep[n & 3][n >> 2] = 0;
            }
        }
        // 2c. the noise: the profile less the peaks' neighbourhoods, and its sample count
        s_t noise = 0;
        ap_uint<9> n_noise = 0;
    NOISE:
        for (int t2 = 0; t2 < 64; ++t2) {
#pragma HLS PIPELINE II=1
            // the row's 4 lanes summed first, then one add onto the loop-carried totals
            s_t row = 0;
            ap_uint<3> cnt = 0;
            for (int m = 0; m < 4; ++m) {
#pragma HLS UNROLL
                if (keep[m][t2]) {
                    row += pdp[m][t2];
                    cnt += 1;
                }
            }
            noise += row;
            n_noise += cnt;
        }
        // 2d. the tests: peak N 2^8 > thr S, and peak 40 > the root's strongest peak (-16 dB)
    DECIDE:
        for (int i = 0; i < nwin; ++i) {
#pragma HLS PIPELINE II=1
            const p_t best = wbest[i];
            const ap_uint<64> lhs = (ap_uint<64>(best) * n_noise) << 8;
            const ap_uint<64> rhs = ap_uint<64>(thr) * ap_uint<64>(noise);
            const ap_uint<44> rel = (ap_uint<44>(best) << 5) + (ap_uint<44>(best) << 3);
            if (lhs > rhs && rel > pmax) {
                DetRec d;
                d.preamble = r * per_root + i;
                d.n_peak = wn[i];
                d.delay = wj[i];
                d.peak = best;
                d.noise = noise;
                d.n_noise = n_noise;
                dets[n_det++] = d;
            }
        }
    }

    // 3. the answer
    Resp resp;
    resp.tx_id = h.tx_id;
    resp.n_det = n_det;
    resp.exp = h.exp;
    streamutils::write_msg<W>(resp, m_out);
OUT:
    for (int i = 0; i < n_det; ++i) {
#pragma HLS PIPELINE II=1
        streamutils::write_msg<W>(dets[i], m_out, i == n_det - 1);
    }
    Status st;
    st.tx_id = h.tx_id;
    st.status = h.code;
    streamutils::write_msg<W>(st, m_status);
    if (h.code != StatusCode::OK) {
        halted = true;
    }
}

#endif  // PRACH_DETECT_TASK_H
