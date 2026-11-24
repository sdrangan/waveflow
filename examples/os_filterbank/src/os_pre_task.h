#ifndef EXAMPLES_OS_FILTERBANK_PRE_TASK_H
#define EXAMPLES_OS_FILTERBANK_PRE_TASK_H
// os_pre_task.h -- the stage in front of the shared FFT (HAND-WRITTEN), the HLS twin of
// examples/os_filterbank/os_filterbank.py::OsPre.  ONE FIRING = ONE FRAME from the arbiter:
//
//   1. the arbiter's index (s_sel, one word before each frame): input 0 is the feedback, so this
//      frame is a product going back through the FFT -- the inverse;
//   2. the frame's Tag, forwarded to post on the tag link beside the FFT (the FFT frames by count
//      and carries nothing through);
//   3. the direction to the FFT's s_dir, then the NW sample words to its s_in -- raw words, already
//      the FFT's input RadixWord.
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "os_filterbank_config.h"
#include "tag.h"

template <int NW, typename TF, typename TS, typename TT>
static void os_pre_task(hls::stream<TF>& s_frm, hls::stream<TS>& s_sel,
                        hls::stream<ap_uint<OS_W_LOOP> >& m_x, hls::stream<ap_uint<8> >& m_dir,
                        hls::stream<TT>& m_tag) {
    const int W = OS_W_LOOP;
    bool last;
    const ap_uint<OS_W_LOOP> sel = streamutils::read_boundary_word<TS, W>(s_sel, last);
    Tag t;
    streamutils::read_msg<OS_W_LOOP>(t, s_frm);
    streamutils::write_msg<OS_W_LOOP>(t, m_tag);
    m_dir.write(sel == 0 ? ap_uint<8>(1) : ap_uint<8>(0));
COPY:
    for (int i = 0; i < NW; ++i) {
#pragma HLS PIPELINE II=1
        m_x.write(streamutils::read_boundary_word<TF, W>(s_frm, last));
    }
}

#endif  // EXAMPLES_OS_FILTERBANK_PRE_TASK_H
