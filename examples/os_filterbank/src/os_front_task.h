#ifndef EXAMPLES_OS_FILTERBANK_FRONT_TASK_H
#define EXAMPLES_OS_FILTERBANK_FRONT_TASK_H
// os_front_task.h -- one channel's front (HAND-WRITTEN), the HLS twin of
// examples/os_filterbank/os_filterbank.py::OsFront.  ONE FIRING = at most one block:
//
//   1. no job open: read its Job header (s_in) and clear the overlap -- each job is its own signal;
//   2. CR blocks of this channel already in the loop: take a credit back (s_crd), or return and
//      re-fire -- polled with empty(), never a blocking read after this channel's last write;
//   3. read the block's BW new words, THEN write the frame -- Tag, the PW overlap words, the BW new
//      words -- so the arbiter never commits to a frame that is still arriving (store and forward).
//
// The words are moved raw: they are already the FFT's input RadixWord (packed by the schema on the
// client's side), and a block is the last PW of the one before followed by the new BW.
//
// CH is a template argument, so each channel's front is its own instantiation with its own statics.
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "os_filterbank_config.h"
#include "job.h"
#include "tag.h"
#include "crd.h"

template <int CH, int CR, typename TI, typename TC, typename TB>
static void os_front_task(hls::stream<TI>& s_in, hls::stream<TC>& s_crd, hls::stream<TB>& m_blk) {
    const int W = OS_W_LOOP;
    static ap_uint<8> left = 0;                 // blocks still to send of the open job
    static ap_uint<8> blk = 0, nblk = 0;
    static ap_uint<16> tx = 0;
    static ap_uint<4> inflight = 0;             // this channel's blocks in the loop
#pragma HLS reset variable=left
#pragma HLS reset variable=inflight
    static ap_uint<OS_W_LOOP> hist[OS_PW];      // the overlap: the last PW words of the block before

    // 1. a job
    if (left == 0) {
        if (s_in.empty()) return;
        Job j;
        streamutils::read_msg<OS_W_LOOP>(j, s_in);
        left = j.nblk;
        nblk = j.nblk;
        tx = j.tx_id;
        blk = 0;
    CLEAR:
        for (int i = 0; i < OS_PW; ++i) hist[i] = 0;
        return;
    }

    // 2. a credit, when this channel holds CR blocks in the loop
    if (inflight == CR) {
        if (s_crd.empty()) return;
        Crd c;
        streamutils::read_msg<OS_W_MSG>(c, s_crd);
        inflight = inflight - 1;
    }

    // 3. the block: read it whole, then the frame
    if (s_in.empty()) return;
    ap_uint<OS_W_LOOP> buf[OS_BW];
READ:
    for (int i = 0; i < OS_BW; ++i) {
#pragma HLS PIPELINE II=1
        bool last;
        buf[i] = streamutils::read_boundary_word<TI, W>(s_in, last);
    }
    Tag t;
    t.tx_id = tx;
    t.ch = CH;
    t.blk = blk;
    t.nblk = nblk;
    t.npass = 0;
    streamutils::write_msg<OS_W_LOOP>(t, m_blk);
WRITE:
    for (int i = 0; i < OS_NW; ++i) {
#pragma HLS PIPELINE II=1
        const ap_uint<OS_W_LOOP> d = (i < OS_PW) ? hist[i] : buf[i - OS_PW];
        streamutils::write_boundary_word<TB, W>(m_blk, d, i == OS_NW - 1);
    }
KEEP:
    for (int i = 0; i < OS_PW; ++i) hist[i] = buf[OS_BW - OS_PW + i];
    inflight = inflight + 1;
    blk = blk + 1;
    left = left - 1;
}

#endif  // EXAMPLES_OS_FILTERBANK_FRONT_TASK_H
