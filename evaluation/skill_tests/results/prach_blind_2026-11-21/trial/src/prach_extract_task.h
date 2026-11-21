#ifndef PRACH_EXTRACT_TASK_H
#define PRACH_EXTRACT_TASK_H
// prach_extract_task.h -- the searcher's second stage (HAND-WRITTEN), the HLS twin of
// prach/prach.py::Extract.run_iter and prach/golden.py::extract_combine.  ONE FIRING = ONE COMMAND:
//
//   1. read FwdHdr and (when nframes > 0) the roots from front;
//   2. read nframes FFT frames (256 words of 4 bins, ap_fixed<27,13>) a word a cycle: bin b of
//      antenna a's window adds into A[a][j], j = (b - k_start) mod 1024, when j < 139 -- the first
//      repetition writes, the others add (exact: ap_fixed<31,17>); the last repetition's pass
//      tracks max |A| over every antenna and bin;
//   3. read FwdTrl; on an error, ExtHdr(code) alone, then halt;
//   4. the block exponent e = #{t in 0..15 : max|A| >= 2^(1+t)} (the smallest e with
//      max|A| / 2^e < 2, i.e. stored < 2^15), then ExtHdr, the roots and Y = A / 2^e assigned to
//      ap_fixed<16,2,AP_RND,AP_SAT> -- antenna-major, 140 bins each (bin 139 zero), 4 a word.
//
// The lanes are rotated by k_start before the accumulator, so lane m always lands in bank m of a
// cyclic-4 accumulator (j = 4w + ((m + k0) & 3) - k0 is m mod 4): four writes a cycle, one a bank.
//
// NOT COMPILED HERE: Vitis HLS is not installed on the machine this was written on.
#include "hls_stream.h"
#include <ap_int.h>
#include <ap_fixed.h>
#include "streamutils_hls.h"
#include "uint8_array_utils.h"
#include "complex__fixed16_2_array_utils.h"
#include "complex__fixed27_13_array_utils.h"
#include "status_code.h"
#include "fwd_hdr.h"
#include "fwd_trl.h"
#include "ext_hdr.h"

#ifndef PRACH_MAX_ROOTS
#define PRACH_MAX_ROOTS 64
#endif

template <int W, typename TC, typename TY>
static void prach_extract_task(hls::stream<TC>& s_cmd, hls::stream<ap_uint<216> >& s_fft,
                               hls::stream<TY>& m_y) {
    static_assert(W == 128, "prach_extract_task: 4 complex int16 a word on m_y");
    static bool halted = false;
#pragma HLS reset variable=halted
    if (halted) {
        return;
    }
    namespace u8 = uint8_array_utils;
    namespace xin = complex__fixed27_13_array_utils;       // the FFT's output lanes
    namespace xy = complex__fixed16_2_array_utils;         // Y's lanes
    typedef ap_fixed<31, 17> acc_t;                        // 27-bit bins, up to 12 summed: exact
    typedef ap_fixed<46, 17> wide_t;                       // acc_t / 2^e, e <= 15: exact
    typedef ap_fixed<16, 2, AP_RND, AP_SAT> y_t;           // Y: sat16(rnd(.))
    const int PF8 = u8::lane_capacity<W>();

    // 1. the command and the roots
    FwdHdr h;
    streamutils::read_msg<W>(h, s_cmd);
    const int nframes = h.nframes;
    const int n_ant = h.n_ant;
    const int n_roots = h.n_roots;
    const ap_uint<10> k0 = h.k_start;
    ap_uint<8> roots[2 * PRACH_MAX_ROOTS];
#pragma HLS ARRAY_PARTITION variable=roots cyclic factor=16 dim=1
    const int nb = 2 * n_roots;
    if (nframes > 0) {
    ROOTS:
        for (int i = 0; i < nb; i += PF8) {
#pragma HLS PIPELINE II=1
            u8::value_type lane[PF8];
#pragma HLS ARRAY_PARTITION variable=lane complete dim=1
            streamutils::tlast_status ts = streamutils::tlast_status::no_tlast;
            const int m = (nb - i < PF8) ? nb - i : PF8;
            u8::read_boundary_lane<W>(s_cmd, lane, m, ts);
            for (int k = 0; k < PF8; ++k) {
#pragma HLS UNROLL
                if (k < m) {
                    roots[i + k] = lane[k];
                }
            }
        }
    }

    // 2. the frames: accumulate the 139 PRACH bins of each antenna, bank m = j mod 4, row j / 4
    acc_t ar[4][4][35];
    acc_t ai[4][4][35];
#pragma HLS ARRAY_PARTITION variable=ar complete dim=2
#pragma HLS ARRAY_PARTITION variable=ai complete dim=2
    ap_ufixed<30, 16> amax = 0;
    const int nwords = nframes * 256;
    int a = 0;            // antenna of the current frame
    int f = 0;            // frame index
    int w = 0;            // word within the frame
FRAMES:
    for (int i = 0; i < nwords; ++i) {
#pragma HLS PIPELINE II=1
#pragma HLS DEPENDENCE variable=ar inter false
#pragma HLS DEPENDENCE variable=ai inter false
        xin::value_type x[4];
#pragma HLS ARRAY_PARTITION variable=x complete dim=1
        xin::read_stream_lane<216>(s_fft, x);
        const bool first = f < n_ant;                  // repetition 0: write, not add
        const bool last_rep = f >= nframes - n_ant;    // the final values: track the maximum
        // this word's largest |component| (0 if it updates nothing), reduced as a tree below so the
        // loop-carried compare with amax is one compare, not eight in series
        ap_ufixed<30, 16> mag[8];
#pragma HLS ARRAY_PARTITION variable=mag complete dim=1
        for (int m = 0; m < 4; ++m) {
#pragma HLS UNROLL
            const ap_uint<2> l = ap_uint<2>(m) + ap_uint<2>(k0);     // the lane that lands in bank m
            const ap_uint<10> b = ap_uint<10>(4 * w) + l;            // its bin
            const ap_uint<10> j = b - k0;                            // mod 1024
            mag[2 * m] = 0;
            mag[2 * m + 1] = 0;
            if (j < 139) {
                const int row = j >> 2;
                const acc_t pr = first ? acc_t(0) : ar[a][m][row];
                const acc_t pi = first ? acc_t(0) : ai[a][m][row];
                const acc_t nr = pr + x[l].real();
                const acc_t ni = pi + x[l].imag();
                ar[a][m][row] = nr;
                ai[a][m][row] = ni;
                if (last_rep) {
                    mag[2 * m] = (nr < 0) ? acc_t(-nr) : nr;
                    mag[2 * m + 1] = (ni < 0) ? acc_t(-ni) : ni;
                }
            }
        }
        const ap_ufixed<30, 16> m01 = mag[0] > mag[1] ? mag[0] : mag[1];
        const ap_ufixed<30, 16> m23 = mag[2] > mag[3] ? mag[2] : mag[3];
        const ap_ufixed<30, 16> m45 = mag[4] > mag[5] ? mag[4] : mag[5];
        const ap_ufixed<30, 16> m67 = mag[6] > mag[7] ? mag[6] : mag[7];
        const ap_ufixed<30, 16> m03 = m01 > m23 ? m01 : m23;
        const ap_ufixed<30, 16> m47 = m45 > m67 ? m45 : m67;
        const ap_ufixed<30, 16> mw = m03 > m47 ? m03 : m47;
        if (mw > amax) {
            amax = mw;
        }
        if (++w == 256) {
            w = 0;
            ++f;
            if (++a == n_ant) a = 0;
        }
    }

    // 3. how front's part ended
    FwdTrl t;
    streamutils::read_msg<W>(t, s_cmd);
    ExtHdr e;
    e.tx_id = h.tx_id;
    e.n_ant = h.n_ant;
    e.n_cs = h.n_cs;
    e.n_pre = h.n_pre;
    e.n_roots = h.n_roots;
    e.thr_q8 = h.thr_q8;
    e.code = t.code;
    if (t.code != StatusCode::OK) {
        e.exp = 0;
        streamutils::write_msg<W>(e, m_y);
        halted = true;
        return;
    }

    // 4. the block exponent, the header, the roots, Y
    int ex = 0;
EXP:
    for (int k = 0; k < 16; ++k) {
#pragma HLS UNROLL
        const ap_ufixed<31, 17> lim = ap_ufixed<31, 17>(2) << k;     // 2^(1+k)
        if (amax >= lim) ex = k + 1;
    }
    e.exp = ex;
    streamutils::write_msg<W>(e, m_y);
FWD_ROOTS:
    for (int i = 0; i < nb; i += PF8) {
#pragma HLS PIPELINE II=1
        u8::value_type lane[PF8];
#pragma HLS ARRAY_PARTITION variable=lane complete dim=1
        const int m = (nb - i < PF8) ? nb - i : PF8;
        for (int k = 0; k < PF8; ++k) {
#pragma HLS UNROLL
            lane[k] = (k < m) ? roots[i + k] : ap_uint<8>(0);
        }
        u8::write_boundary_lane<W>(lane, m_y, i + PF8 >= nb, m);
    }
    const int ny = n_ant * 35;                           // 140 bins an antenna, 4 a word
    int ya = 0, row = 0;
Y:
    for (int i = 0; i < ny; ++i) {
#pragma HLS PIPELINE II=1
        xy::value_type y[4];
#pragma HLS ARRAY_PARTITION variable=y complete dim=1
        for (int m = 0; m < 4; ++m) {
#pragma HLS UNROLL
            const int j = 4 * row + m;
            if (j < 139) {
                wide_t vr = ar[ya][m][row];
                wide_t vi = ai[ya][m][row];
                vr = vr >> ex;
                vi = vi >> ex;
                y[m] = xy::value_type(y_t(vr), y_t(vi));
            } else {
                y[m] = xy::value_type(0, 0);              // bin 139: the pad
            }
        }
        xy::write_boundary_lane<W>(y, m_y, i == ny - 1, 4);
        if (++row == 35) {
            row = 0;
            ++ya;
        }
    }
}

#endif  // PRACH_EXTRACT_TASK_H
