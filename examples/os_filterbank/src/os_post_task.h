#ifndef EXAMPLES_OS_FILTERBANK_POST_TASK_H
#define EXAMPLES_OS_FILTERBANK_POST_TASK_H
// os_post_task.h -- the stage after the shared FFT (HAND-WRITTEN), the HLS twin of
// examples/os_filterbank/os_filterbank.py::OsPost.  ONE FIRING:
//
//   0. until all C filters are loaded: one filter off s_coef -- Coef(ch), then NW words of
//      H_c / H_SCALE -- into slot ch.  Resident: loaded once, never sent again;
//   1. otherwise one frame out of the FFT: its Tag (beside the FFT: tag i belongs to frame i, since
//      the FFT keeps order), then its NW words;
//   2. pass 0 (forward bins): X * H_c into the FFT's input format, the whole product first, then the
//      feedback frame -- Tag (npass 1), the NW product words -- to the arbiter's input 0;
//   3. pass 1 (the inverse): the answer -- Resp, the last BW words (the overlap dropped, the words
//      raw: the inverse's bits, read as inv_out_fmt) -- a Status after the job's last block, and a
//      credit to the channel.
//
// Post never blocks on m_fb: a channel sends a block only with a credit, so at most C*CR blocks are
// in the loop and the feedback link holds a frame for each (os_filterbank.py, DEPTH).
//
// The multiply is ssr_fft_hls::ssr_cmul -- the library's complexMultiply, partial products in the
// bins' format -- into the FFT's input format; the golden is waveflow.vitis_l1.cxquant's model of it.
// Every word is packed by the generated lane routines (os_fft::e_out / os_cx_in), never by hand.
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "os_filterbank_config.h"
#include "ssr_fft_tasks.h"
#include "tag.h"
#include "coef.h"
#include "resp.h"
#include "status.h"
#include "crd.h"

template <int C, int NW, int PW, typename TCo, typename TT, typename TF, typename TA, typename TSt,
          typename TCr>
static void os_post_task(hls::stream<TCo>& s_coef, hls::stream<TT>& s_tag,
                         hls::stream<ap_uint<OS_W_ANS> >& s_y, hls::stream<TF>& m_fb,
                         hls::stream<TA>& m_ans, hls::stream<TSt>& m_st, hls::stream<TCr>& m_crd) {
    typedef os_fft::e_in EI;
    typedef os_fft::e_out EO;
    typedef typename EI::value_type in_c;
    typedef typename EO::value_type out_c;
    typedef typename in_c::value_type in_t;
    typedef typename out_c::value_type out_t;
    const int WA = OS_W_ANS;

    static in_c hs[C][NW][4];                    // the slots: H_c / H_SCALE, bin by bin
#pragma HLS ARRAY_PARTITION variable = hs dim = 3 complete
    static ap_uint<4> nloaded = 0;
#pragma HLS reset variable=nloaded

    // 0. the filters, once
    if (nloaded < C) {
        Coef c;
        streamutils::read_msg<OS_W_LOOP>(c, s_coef);
    LOAD:
        for (int w = 0; w < NW; ++w) {
#pragma HLS PIPELINE II=1
            in_c v[4];
            streamutils::tlast_status ts = streamutils::tlast_status::no_tlast;
            os_cx_in::read_boundary_lane<OS_W_LOOP>(s_coef, v, 4, ts);
            for (int q = 0; q < 4; ++q) hs[c.ch][w][q] = v[q];
        }
        nloaded = nloaded + 1;
        return;
    }

    // 1. a frame: its tag, then its words
    Tag t;
    streamutils::read_msg<OS_W_LOOP>(t, s_tag);
    const int ch = t.ch;

    // 2. pass 0: the product, whole, then back to the arbiter
    if (t.npass == 0) {
        in_c buf[NW][4];
#pragma HLS ARRAY_PARTITION variable = buf dim = 2 complete
    MUL:
        for (int w = 0; w < NW; ++w) {
#pragma HLS PIPELINE II=1
            out_c y[4];
            EO::read(s_y, y);
            for (int q = 0; q < 4; ++q) buf[w][q] = ssr_fft_hls::ssr_cmul<out_t, in_t, in_t>(y[q], hs[ch][w][q]);
        }
        Tag f = t;
        f.npass = 1;
        streamutils::write_msg<OS_W_LOOP>(f, m_fb);
    FB:
        for (int w = 0; w < NW; ++w) {
#pragma HLS PIPELINE II=1
            in_c v[4];
            for (int q = 0; q < 4; ++q) v[q] = buf[w][q];
            os_cx_in::write_boundary_lane<OS_W_LOOP>(v, m_fb, w == NW - 1, 4);
        }
        return;
    }

    // 3. pass 1: the answer, a status after the job's last block, the credit
    Resp r;
    r.ch = t.ch;
    r.tx_id = t.tx_id;
    r.blk = t.blk;
    streamutils::write_msg<OS_W_ANS>(r, m_ans);
OUT:
    for (int w = 0; w < NW; ++w) {
#pragma HLS PIPELINE II=1
        const ap_uint<OS_W_ANS> d = s_y.read();
        if (w >= PW) streamutils::write_boundary_word<TA, WA>(m_ans, d, w == NW - 1);
    }
    if (t.blk == t.nblk - 1) {
        Status s;
        s.ch = t.ch;
        s.tx_id = t.tx_id;
        s.status = StatusCode::OK;
        streamutils::write_msg<OS_W_MSG>(s, m_st);
    }
    Crd k;
    k.ch = t.ch;
    streamutils::write_msg<OS_W_MSG>(k, m_crd);
}

#endif  // EXAMPLES_OS_FILTERBANK_POST_TASK_H
