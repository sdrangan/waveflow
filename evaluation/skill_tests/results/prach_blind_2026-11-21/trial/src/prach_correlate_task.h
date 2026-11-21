#ifndef PRACH_CORRELATE_TASK_H
#define PRACH_CORRELATE_TASK_H
// prach_correlate_task.h -- the searcher's third stage (HAND-WRITTEN), the HLS twin of
// prach/prach.py::Correlate.run_iter and prach/golden.py::correlate.  ONE FIRING = ONE COMMAND:
//
//   1. read ExtHdr; write CorHdr (detect's copy of the command -- always, errors included);
//      on an error, halt;
//   2. read the roots and Y (n_ant x 140 bins, 4 a word) into banks: bin j in bank j mod 4;
//   3. per root r, per antenna a, 64 words to the FFT, a word a cycle: bin k = 4t + m is
//      Z = conj(Y[a][k]) R_u(k) / 2 as ap_fixed<16,2,AP_RND,AP_SAT> for k < 139, zero above, with
//      R_u(k) = ROM[p], p = u m (m + 1) mod 278, m = u_inv k mod 139 (prach_rom.h, generated).
//
// The product is exact (ap_fixed<33,4>), widened to <34,4> so that the halving is exact, then rounded
// and saturated once on assignment -- golden.correlate's sat16(rnd((yr rr + yi ri) / 2^16)).
//
// NOT COMPILED HERE: Vitis HLS is not installed on the machine this was written on.
#include "hls_stream.h"
#include <ap_int.h>
#include <ap_fixed.h>
#include "streamutils_hls.h"
#include "uint8_array_utils.h"
#include "complex__fixed16_2_array_utils.h"
#include "status_code.h"
#include "ext_hdr.h"
#include "cor_hdr.h"
#include "prach_rom.h"

#ifndef PRACH_MAX_ROOTS
#define PRACH_MAX_ROOTS 64
#endif

template <int W, typename TY, typename TC>
static void prach_correlate_task(hls::stream<TY>& s_y, hls::stream<TC>& m_cmd,
                                 hls::stream<ap_uint<W> >& m_fft) {
    static_assert(W == 128, "prach_correlate_task: 4 complex int16 a word");
    static bool halted = false;
#pragma HLS reset variable=halted
    if (halted) {
        return;
    }
    namespace u8 = uint8_array_utils;
    namespace xy = complex__fixed16_2_array_utils;
    typedef ap_fixed<16, 2> y_t;                       // Y (stored as read)
    typedef ap_fixed<34, 4> p_t;                       // the product, one spare fraction bit
    typedef ap_fixed<16, 2, AP_RND, AP_SAT> z_t;
    const int PF8 = u8::lane_capacity<W>();

    // 1. the command, forwarded first: detect waits on it before the FFT's frames
    ExtHdr h;
    streamutils::read_msg<W>(h, s_y);
    CorHdr c;
    c.tx_id = h.tx_id;
    c.n_ant = h.n_ant;
    c.n_cs = h.n_cs;
    c.n_pre = h.n_pre;
    c.n_roots = h.n_roots;
    c.thr_q8 = h.thr_q8;
    c.exp = h.exp;
    c.code = h.code;
    streamutils::write_msg<W>(c, m_cmd);
    if (h.code != StatusCode::OK) {
        halted = true;
        return;
    }
    const int n_ant = h.n_ant;
    const int n_roots = h.n_roots;

    // 2. the roots, then Y into banks
    ap_uint<8> roots[2 * PRACH_MAX_ROOTS];
#pragma HLS ARRAY_PARTITION variable=roots cyclic factor=16 dim=1
    const int nb = 2 * n_roots;
ROOTS:
    for (int i = 0; i < nb; i += PF8) {
#pragma HLS PIPELINE II=1
        u8::value_type lane[PF8];
#pragma HLS ARRAY_PARTITION variable=lane complete dim=1
        streamutils::tlast_status ts = streamutils::tlast_status::no_tlast;
        const int m = (nb - i < PF8) ? nb - i : PF8;
        u8::read_boundary_lane<W>(s_y, lane, m, ts);
        for (int k = 0; k < PF8; ++k) {
#pragma HLS UNROLL
            if (k < m) {
                roots[i + k] = lane[k];
            }
        }
    }
    y_t yr[4][4][35];
    y_t yi[4][4][35];
#pragma HLS ARRAY_PARTITION variable=yr complete dim=2
#pragma HLS ARRAY_PARTITION variable=yi complete dim=2
    const int ny = n_ant * 35;
    int ya = 0, row = 0;
Y:
    for (int i = 0; i < ny; ++i) {
#pragma HLS PIPELINE II=1
        xy::value_type y[4];
#pragma HLS ARRAY_PARTITION variable=y complete dim=1
        streamutils::tlast_status ts = streamutils::tlast_status::no_tlast;
        xy::read_boundary_lane<W>(s_y, y, 4, ts);
        for (int m = 0; m < 4; ++m) {
#pragma HLS UNROLL
            yr[ya][m][row] = y[m].real();
            yi[ya][m][row] = y[m].imag();
        }
        if (++row == 35) {
            row = 0;
            ++ya;
        }
    }

    // 3. per root, per antenna: a 256-point frame, 4 bins a word
    const int nwords = n_roots * n_ant * 64;
    int r = 0, a = 0, t = 0;
CORR:
    for (int i = 0; i < nwords; ++i) {
#pragma HLS PIPELINE II=1
        const ap_uint<8> u = roots[2 * r];
        const ap_uint<8> ui = roots[2 * r + 1];
        xy::value_type z[4];
#pragma HLS ARRAY_PARTITION variable=z complete dim=1
        for (int m = 0; m < 4; ++m) {
#pragma HLS UNROLL
            const int k = 4 * t + m;
            if (k < 139) {
                const ap_uint<8> mm = (ap_uint<16>(ui) * ap_uint<16>(k)) % 139;
                const ap_uint<9> p = (ap_uint<22>(u) * (ap_uint<16>(mm) * ap_uint<16>(mm + 1))) % 278;
                const prach_rom::r_t rr = prach_rom::cos_at(m, p);    // lane m's own ROM
                const prach_rom::r_t ri = prach_rom::sin_at(m, p);
                const y_t a_r = yr[a][m][t];
                const y_t a_i = yi[a][m][t];
                p_t pr = a_r * rr + a_i * ri;                // conj(Y) R, real
                p_t pi = a_r * ri - a_i * rr;                // conj(Y) R, imag
                pr = pr >> 1;
                pi = pi >> 1;
                z[m] = xy::value_type(z_t(pr), z_t(pi));
            } else {
                z[m] = xy::value_type(0, 0);                 // the zero pad, 139..255
            }
        }
        xy::write_stream_lane<W>(z, m_fft);
        if (++t == 64) {
            t = 0;
            if (++a == n_ant) {
                a = 0;
                ++r;
            }
        }
    }
}

#endif  // PRACH_CORRELATE_TASK_H
