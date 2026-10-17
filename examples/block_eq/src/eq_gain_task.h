#ifndef EXAMPLES_BLOCK_EQ_GAIN_TASK_H
#define EXAMPLES_BLOCK_EQ_GAIN_TASK_H
// eq_gain_task.h -- the equalizer's first stage (HAND-WRITTEN), the HLS twin of
// examples/block_eq/block_eq.py::Gain.run_iter.  ONE FIRING = ONE COMMAND:
//
//   1. read the CmdHdr and the estimates (three float32s a row: hr, hi, N), into a buffer;
//   2. per row, g = conj(h) / (|h|^2 + N) in the golden's operation order, one statement an operation
//      (no fused multiply-add); the first N <= 0 is BAD_NOISE;
//   3. send GainHdr(tx_id, nvalid, code), then the nvalid g values, to apply;
//   4. KEEP CONSUMING: read and discard this copy of the symbols -- they are apply's, but the broadcast
//      writes both copies in lockstep, so an unread copy would stall it (docs/guide/streams/broadcast.md).
//
// An error is fatal: after BAD_NOISE, estimates ending early, or symbols ending early, `halted` is set
// and every later firing returns at once.  W is 64: two float32s, or one complex value, a word.
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "float32_array_utils.h"
#include "cmd_hdr.h"
#include "gain_hdr.h"


#ifndef EQ_MAX_ROWS
#define EQ_MAX_ROWS 64
#endif

template <int W, typename TI, typename TO>
static void eq_gain_task(hls::stream<TI>& s_in, hls::stream<TO>& m_g) {
    static_assert(W == 64, "eq_gain_task: two float32s a word");
    static bool halted = false;
#pragma HLS reset variable=halted
    if (halted) {
        return;
    }

    // 1. the command and the estimates
    CmdHdr hdr;
    streamutils::read_msg<W>(hdr, s_in);
    const int nrows = hdr.nrows;
    const int m = hdr.m;
    const int nf = 3 * nrows;
    float est[3 * EQ_MAX_ROWS];
#pragma HLS ARRAY_PARTITION variable=est cyclic factor=6 dim=1
    float lane[2];
#pragma HLS ARRAY_PARTITION variable=lane complete dim=1
    int got = 0;
    bool est_early = false;
EST:
    for (int i = 0; i < nf; i += 2) {
#pragma HLS PIPELINE II=1
        streamutils::tlast_status ts = streamutils::tlast_status::no_tlast;
        float32_array_utils::read_boundary_lane<W>(s_in, lane, nf - i, ts);
        est[i] = lane[0];
        if (i + 1 < 3 * EQ_MAX_ROWS) {
            est[i + 1] = lane[1];
        }
        got = i + 2;
        if (ts == streamutils::tlast_status::tlast_at_end && nf - i > 2) {
            est_early = true;
            break;
        }
    }

    // 2. g, row by row
    const int complete = est_early ? (got / 3) : nrows;
    int nvalid = complete;
    StatusCode code = est_early ? StatusCode::TLAST_EARLY : StatusCode::OK;
    bool found = false;
    float gr[EQ_MAX_ROWS];
    float gi[EQ_MAX_ROWS];
ROWS:
    for (int r = 0; r < complete; ++r) {
#pragma HLS PIPELINE II=1
        const float hr = est[3 * r];
        const float hi = est[3 * r + 1];
        const float n = est[3 * r + 2];
        const float t1 = hr * hr;
        const float t2 = hi * hi;
        float d = t1 + t2;
        d = d + n;
        const float q = 1.0f / d;
        gr[r] = hr * q;
        const float t = hi * q;
        gi[r] = -t;
        if (!found && !(n > 0.0f)) {
            found = true;
            nvalid = r;
            code = StatusCode::BAD_NOISE;
        }
    }

    // 3. to apply: the header, then the g values
    GainHdr gh;
    gh.tx_id = hdr.tx_id;
    gh.nvalid = nvalid;
    gh.code = code;
    streamutils::write_msg<W>(gh, m_g);
G:
    for (int r = 0; r < nvalid; ++r) {
#pragma HLS PIPELINE II=1
        float out[2];
#pragma HLS ARRAY_PARTITION variable=out complete dim=1
        out[0] = gr[r];
        out[1] = gi[r];
        float32_array_utils::write_boundary_lane<W>(out, m_g, r == nvalid - 1, 2);
    }

    // 4. keep consuming: this copy of the symbols, discarded
    const int ns = 2 * nrows * m;
    bool sym_early = false;
SYM:
    for (int i = 0; i < ns; i += 2) {
#pragma HLS PIPELINE II=1
        streamutils::tlast_status ts = streamutils::tlast_status::no_tlast;
        float32_array_utils::read_boundary_lane<W>(s_in, lane, ns - i, ts);
        if (ts == streamutils::tlast_status::tlast_at_end && ns - i > 2) {
            sym_early = true;
            break;
        }
    }

    if (code != StatusCode::OK || sym_early) {
        halted = true;
    }
}

#endif  // EXAMPLES_BLOCK_EQ_GAIN_TASK_H
