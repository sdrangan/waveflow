#ifndef EXAMPLES_BLOCK_EQ_APPLY_TASK_H
#define EXAMPLES_BLOCK_EQ_APPLY_TASK_H
// eq_apply_task.h -- the equalizer's second stage (HAND-WRITTEN), the HLS twin of
// examples/block_eq/block_eq.py::Apply.run_iter.  ONE FIRING = ONE COMMAND:
//
//   1. read the CmdHdr, then -- KEEP CONSUMING, FIRST -- read and discard this copy of the estimates.
//      Waiting on gain before this would deadlock: the broadcast would stall on this full copy, and
//      gain would never get the rest of the estimates (docs/guide/streams/broadcast.md);
//   2. read GainHdr and the nvalid g values from gain;
//   3. write the Resp header, then the symbols a word per iteration at II=1: z = g r in the golden's
//      operation order, one statement an operation; the last result carries TLAST;
//   4. write the Status: the last stage answers.  An error is fatal: `halted` is set.
//
// W is 64: one complex value (two float32s) a word.
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "float32_array_utils.h"
#include "cmd_hdr.h"
#include "gain_hdr.h"
#include "resp.h"
#include "status.h"


#ifndef EQ_MAX_ROWS
#define EQ_MAX_ROWS 64
#endif

template <int W, typename TI, typename TG, typename TO, typename TS>
static void eq_apply_task(hls::stream<TI>& s_in, hls::stream<TG>& s_g, hls::stream<TO>& m_out,
                          hls::stream<TS>& m_status) {
    static_assert(W == 64, "eq_apply_task: one complex value a word");
    static bool halted = false;
#pragma HLS reset variable=halted
    if (halted) {
        return;
    }

    // 1. the command, then this copy of the estimates, discarded
    CmdHdr hdr;
    streamutils::read_msg<W>(hdr, s_in);
    const int nrows = hdr.nrows;
    const int m = hdr.m;
    const int nf = 3 * nrows;
    float lane[2];
#pragma HLS ARRAY_PARTITION variable=lane complete dim=1
EST:
    for (int i = 0; i < nf; i += 2) {
#pragma HLS PIPELINE II=1
        streamutils::tlast_status ts = streamutils::tlast_status::no_tlast;
        float32_array_utils::read_boundary_lane<W>(s_in, lane, nf - i, ts);
        if (ts == streamutils::tlast_status::tlast_at_end && nf - i > 2) {
            break;
        }
    }

    // 2. g, from gain
    GainHdr gh;
    streamutils::read_msg<W>(gh, s_g);
    const int nvalid = gh.nvalid;
    float gr[EQ_MAX_ROWS];
    float gi[EQ_MAX_ROWS];
G:
    for (int r = 0; r < nvalid; ++r) {
#pragma HLS PIPELINE II=1
        streamutils::tlast_status ts = streamutils::tlast_status::no_tlast;
        float32_array_utils::read_boundary_lane<W>(s_g, lane, 2 * (nvalid - r), ts);
        gr[r] = lane[0];
        gi[r] = lane[1];
    }

    // 3. the header, then z = g r, a symbol a word
    Resp resp;
    resp.tx_id = hdr.tx_id;
    resp.nrows = hdr.nrows;
    resp.m = hdr.m;
    streamutils::write_msg<W>(resp, m_out);
    const int need = nvalid * m;
    const int total = nrows * m;
    int done = 0;
    bool early = false;
    int row = 0;
    int col = 0;
SYM:
    for (int i = 0; i < need; ++i) {
#pragma HLS PIPELINE II=1
        streamutils::tlast_status ts = streamutils::tlast_status::no_tlast;
        float32_array_utils::read_boundary_lane<W>(s_in, lane, 2 * (total - i), ts);
        const bool cut = (ts == streamutils::tlast_status::tlast_at_end) && (i < total - 1);
        const float g_r = gr[row];
        const float g_i = gi[row];
        const float p1 = g_r * lane[0];
        const float p2 = g_i * lane[1];
        const float p3 = g_r * lane[1];
        const float p4 = g_i * lane[0];
        float z[2];
#pragma HLS ARRAY_PARTITION variable=z complete dim=1
        z[0] = p1 - p2;
        z[1] = p3 + p4;
        float32_array_utils::write_boundary_lane<W>(z, m_out, (i == need - 1) || cut, 2);
        done = i + 1;
        if (++col == m) {
            col = 0;
            ++row;
        }
        if (cut) {
            early = true;
            break;
        }
    }

    // 4. the status: symbols cut before the rows that have a g -> TLAST_EARLY; else gain's code
    Status st;
    st.tx_id = hdr.tx_id;
    st.status = (early && done < need) ? StatusCode::TLAST_EARLY : gh.code;
    streamutils::write_msg<W>(st, m_status);
    if (st.status != StatusCode::OK) {
        halted = true;
    }
}

#endif  // EXAMPLES_BLOCK_EQ_APPLY_TASK_H
