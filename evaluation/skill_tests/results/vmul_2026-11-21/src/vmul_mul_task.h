#ifndef VMUL_MUL_TASK_H
#define VMUL_MUL_TASK_H
// vmul_mul_task.h -- HAND-WRITTEN.  The HLS twin of vmul.py::VmulMul.run_iter.
//
// One firing = one command: read the VmulCmd the issue stage passed on, frame the writer's stream
// [MemWCmd(z_addr, words of z, fwd_bursts=1) | VmulResp | z], z[i] = x[i] * y[i].  The writer holds the
// VmulResp across the write and echoes it on m_done once z is stored.
//
// The loop: one z WORD per iteration (ZPF = 2 int32 at W = 64), at II=1 -- the writer's rate, one word
// a clock.  An x / y word holds XPF = 4 int16, so the loop reads a word from each every XPF/ZPF = 2
// iterations (loop_optimization.md's shape B, with ZPF elements a step instead of one).  The
// multiply is exact: ap_int<16> * ap_int<16> into ap_int<32>, as vmul_golden() does it.
//
// An error is fatal: n == 0 writes nothing, answers BAD_LEN through the writer (MemWCmd len 0) and
// halts until a reset.
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "int16_array_utils.h"
#include "int32_array_utils.h"
#include "vmul_code.h"
#include "vmul_cmd.h"
#include "vmul_resp.h"
#include "mem_w_cmd.h"

template <int W>
static void vmul_mul_task(hls::stream<streamutils::framed_word<W> >& s_desc,
                          hls::stream<streamutils::framed_word<W> >& s_x,
                          hls::stream<streamutils::framed_word<W> >& s_y,
                          hls::stream<streamutils::framed_word<W> >& m_wr) {
    namespace xu = int16_array_utils;
    namespace zu = int32_array_utils;
    static bool halted = false;                  // set by an error, cleared only by a reset
#pragma HLS reset variable=halted
    if (halted) {
        return;
    }
    VmulCmd cmd;
    cmd.read_framed_stream<W>(s_desc);           // a blocking read first
    const int n = (int)cmd.n;

    MemWCmd wc;
    wc.addr = cmd.z_addr;
    wc.fwd_bursts = 1;
    VmulResp r;
    r.tx_id = cmd.tx_id;
    r.n = cmd.n;
    if (n == 0) {
        wc.len = 0;
        wc.write_framed_stream<W>(m_wr);
        r.code = VmulCode::BAD_LEN;
        r.write_framed_stream<W>(m_wr);
        halted = true;
        return;
    }
    wc.len = zu::get_nwords<W>(n);
    wc.write_framed_stream<W>(m_wr);
    r.code = VmulCode::OK;
    r.write_framed_stream<W>(m_wr);

    const int XPF = xu::lane_capacity<W>();
    const int ZPF = zu::lane_capacity<W>();
    const int R = XPF / ZPF;                     // z words per x word
    xu::value_type xl[XPF], yl[XPF];
    zu::value_type zl[ZPF];
#pragma HLS ARRAY_PARTITION variable=xl complete dim=1
#pragma HLS ARRAY_PARTITION variable=yl complete dim=1
#pragma HLS ARRAY_PARTITION variable=zl complete dim=1
MUL:
    for (int i = 0; i < n; i += ZPF) {
#pragma HLS PIPELINE II=1
        const int k = (i / ZPF) % R;             // R a power of two: wiring
        if (k == 0) {                            // a fresh x and y word
            const int mx = (n - i < XPF) ? (n - i) : XPF;
            xu::read_framed_stream_lane<W>(s_x, xl, mx);
            xu::read_framed_stream_lane<W>(s_y, yl, mx);
        }
        const int mz = (n - i < ZPF) ? (n - i) : ZPF;
        for (int t = 0; t < ZPF; ++t) {
#pragma HLS UNROLL
            zl[t] = (zu::value_type)xl[k * ZPF + t] * (zu::value_type)yl[k * ZPF + t];
        }
        zu::write_framed_stream_lane<W>(zl, m_wr, i + ZPF >= n, mz);
    }
}

#endif  // VMUL_MUL_TASK_H
