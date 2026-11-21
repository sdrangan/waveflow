#ifndef PRACH_FRONT_TASK_H
#define PRACH_FRONT_TASK_H
// prach_front_task.h -- the searcher's first stage (HAND-WRITTEN), the HLS twin of
// prach/prach.py::Front.run_iter.  ONE FIRING = ONE COMMAND:
//
//   1. read the PrachCmd (one word; no TLAST on it is TLAST_LATE); check it (check_params, in order);
//   2. read the 2 n_roots root bytes and check them (check_roots); an early TLAST is TLAST_EARLY,
//      none on the last root word TLAST_LATE (decision 20);
//   3. on an error: FwdHdr(nframes = 0), FwdTrl(code), halt -- nothing more is read;
//   4. else FwdHdr(nframes = n_sym n_ant), the roots, then every window to the FFT, a word a cycle;
//      after an early TLAST, or none on a window's word 255, the rest of the job's words are zeros
//      (the FFT is framed by count) and nothing more is read;
//   5. FwdTrl(OK, TLAST_EARLY or TLAST_LATE); halt on an error.
//
// The windows are copied as whole words: front never unpacks a sample, so no lane routine is needed
// on that path (the Python model unpacks and repacks the same bits).
//
// NOT COMPILED HERE: Vitis HLS is not installed on the machine this was written on (see
// results/report.md).  Every line follows the Python model; csim / csynth / XSI are the gates.
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "uint8_array_utils.h"
#include "status_code.h"
#include "prach_cmd.h"
#include "fwd_hdr.h"
#include "fwd_trl.h"

#ifndef PRACH_MAX_ROOTS
#define PRACH_MAX_ROOTS 64
#endif

// The command, with its TLAST status: read_msg has no status out, so pick the schema's own framed /
// AXI-Stream reader by the port's word type (the generated PrachCmd has both, decision 20).
template <int W>
static void prach_read_cmd(PrachCmd& c, hls::stream<streamutils::framed_word<W> >& s,
                           streamutils::tlast_status& tl) {
    c.template read_framed_stream<W>(s, tl);
}
template <int W>
static void prach_read_cmd(PrachCmd& c, hls::stream<streamutils::axi4s_word<W> >& s,
                           streamutils::tlast_status& tl) {
    c.template read_axi4_stream<W>(s, tl);
}

template <int W, typename TI, typename TC>
static void prach_front_task(hls::stream<TI>& s_in, hls::stream<TC>& m_cmd,
                             hls::stream<ap_uint<W> >& m_fft) {
    static_assert(W == 128, "prach_front_task: 4 complex int16 a word");
    static bool halted = false;
#pragma HLS reset variable=halted
    if (halted) {
        return;
    }
    namespace u8 = uint8_array_utils;
    const int PF8 = u8::lane_capacity<W>();          // 16 root bytes a word

    // 1. the command, checked in check_params' order
    PrachCmd c;
    streamutils::tlast_status ctl = streamutils::tlast_status::no_tlast;
    prach_read_cmd<W>(c, s_in, ctl);
    const int n_ant = c.n_ant, n_sym = c.n_sym, n_cs = c.n_cs, n_pre = c.n_pre;
    const int n_roots = c.n_roots;
    StatusCode code = StatusCode::OK;
    if (ctl != streamutils::tlast_status::tlast_at_end) {
        code = StatusCode::TLAST_LATE;                // the one-word command has no TLAST
    } else if (n_ant < 1 || n_ant > 4 || n_sym < 1 || n_sym > 12 || c.k_start >= 1024 || n_cs > 139 ||
        n_pre < 1 || n_pre > 64) {
        code = StatusCode::BAD_PARAM;
    } else {
        const int per_root = (n_cs == 0) ? 1 : 139 / n_cs;
        if (n_roots != (n_pre + per_root - 1) / per_root) {
            code = StatusCode::BAD_PARAM;
        }
    }

    // 2. the roots: (u, u_inv) pairs, 16 bytes a word; every pair checked
    ap_uint<8> roots[2 * PRACH_MAX_ROOTS];
#pragma HLS ARRAY_PARTITION variable=roots cyclic factor=16 dim=1      // 16 bytes a word, a word a cycle
    if (code == StatusCode::OK) {
        const int nb = 2 * n_roots;
        int got = 0;
        bool bad = false;
        bool late = false;
    ROOTS:
        for (int i = 0; i < nb; i += PF8) {
#pragma HLS PIPELINE II=1
            u8::value_type lane[PF8];
#pragma HLS ARRAY_PARTITION variable=lane complete dim=1
            streamutils::tlast_status ts = streamutils::tlast_status::no_tlast;
            const int m = (nb - i < PF8) ? nb - i : PF8;
            u8::read_boundary_lane<W>(s_in, lane, m, ts);
            for (int k = 0; k < PF8; ++k) {
#pragma HLS UNROLL
                if (k < m) {
                    roots[i + k] = lane[k];
                }
            }
            got = i + m;
            const bool tl = ts == streamutils::tlast_status::tlast_at_end;
            if (tl && i + m < nb) {
                break;                                // TLAST before the last root byte
            }
            if (!tl && i + m >= nb) {
                late = true;                          // the last root word without TLAST
            }
        }
    CHECK:
        for (int r = 0; r < n_roots; ++r) {
#pragma HLS PIPELINE II=1
            const ap_uint<8> u = roots[2 * r];
            const ap_uint<8> ui = roots[2 * r + 1];
            const ap_uint<16> prod = u * ui;
            if (u < 1 || u > 138 || (prod % 139) != 1) {
                bad = true;
            }
        }
        if (got < nb) {
            code = StatusCode::TLAST_EARLY;
        } else if (late) {
            code = StatusCode::TLAST_LATE;
        } else if (bad) {
            code = StatusCode::BAD_ROOT;
        }
    }

    FwdHdr h;
    h.tx_id = c.tx_id;
    h.n_ant = c.n_ant;
    h.n_sym = c.n_sym;
    h.k_start = c.k_start;
    h.n_cs = c.n_cs;
    h.n_pre = c.n_pre;
    h.n_roots = c.n_roots;
    h.thr_q8 = c.thr_q8;
    FwdTrl t;
    t.tx_id = c.tx_id;

    // 3. an error before any window: nothing goes to the FFT
    if (code != StatusCode::OK) {
        h.nframes = 0;
        streamutils::write_msg<W>(h, m_cmd);
        t.code = code;
        streamutils::write_msg<W>(t, m_cmd);
        halted = true;
        return;
    }

    // 4. the header, the roots, then the windows: 256 words each, a word a cycle
    const int nframes = n_sym * n_ant;
    h.nframes = nframes;
    streamutils::write_msg<W>(h, m_cmd);
    const int nb = 2 * n_roots;
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
        u8::write_boundary_lane<W>(lane, m_cmd, i + PF8 >= nb, m);
    }
    bool cut = false;                                 // TLAST before a window's word 255
    bool late = false;                                // no TLAST on a window's word 255
    const int nwords = nframes * 256;
WIN:
    for (int i = 0; i < nwords; ++i) {
#pragma HLS PIPELINE II=1
        ap_uint<W> w = 0;
        if (!cut && !late) {
            bool last = false;
            w = streamutils::read_boundary_word<TI, W>(s_in, last);
            const bool end = (i % 256) == 255;
            if (last && !end) {
                cut = true;                           // this word is the window's last: zeros follow
            }
            if (!last && end) {
                late = true;                          // this window whole; zeros follow, the excess unread
            }
        }
        m_fft.write(w);
    }

    // 5. how the job ended
    t.code = cut ? StatusCode::TLAST_EARLY : (late ? StatusCode::TLAST_LATE : StatusCode::OK);
    streamutils::write_msg<W>(t, m_cmd);
    if (cut || late) {
        halted = true;
    }
}

#endif  // PRACH_FRONT_TASK_H
