#ifndef EXAMPLES_MM_FIR_TASK_H
#define EXAMPLES_MM_FIR_TASK_H
// mm_fir_task.h -- the mm_fir kernel body (HAND-WRITTEN), the HLS twin of MmFir.run_iter.
//
// plans/mm_fir_cfg_seq.md: the stream_inband pattern with a config sequence number.  On s_in every
// sample packet is preceded by a one-word FirCmdHdr (nsamp, tx_id, cfg_seq).  Per packet the body
//
//   HDR   reads the header;
//   CFG   takes configs from s_cfg until it has received cfg_seq of them -- WAITING if the packet's
//         config has not arrived (a config nobody has asked for stays in s_cfg);
//   SAMP  filters nsamp samples, one per firing, writing each result to m_out;
//   RESP  writes one FirRespHdr to m_resp: the packet's tx_id and the cfg_seq it was filtered with.
//
// A free-running hls::task body, pipelined at II=1: ONE firing per cycle, and every firing moves AT
// MOST ONE WORD on each stream -- that is what lets the whole body pipeline.  Inputs are read with
// read_nb, so a firing that finds nothing does nothing (and touches no bus: s_in and s_cfg are
// streams from the adaptor beside the kernel).
//
// Status (nsamp, ncfg) is published after every packet: serialized once (FirStatus::write_array) and
// emitted a word per firing; a publish requested while one is going out waits for it.
//
// Numbers: int16 samples (the low 16 bits of each 64-bit word), int16 taps, the exact integer sum in
// an ap_int<64>, written as its two's complement word.  No rounding, so pysim's numpy golden is
// bit-exact by construction.
#include "hls_stream.h"
#include <ap_int.h>
#include "fir_cfg.h"
#include "fir_cmd_hdr.h"
#include "fir_resp_hdr.h"
#include "fir_status.h"

template <int DW>
static void mm_fir_task(hls::stream<ap_uint<DW> >& s_cfg,
                        hls::stream<ap_uint<DW> >& s_in,
                        hls::stream<ap_uint<DW> >& m_out,
                        hls::stream<ap_uint<DW> >& m_resp,
                        hls::stream<ap_uint<DW> >& m_status) {
#pragma HLS PIPELINE II=1
    const int NT = 16;
    const int CW = FirCfg::nwords<DW>();
    const int SW = FirStatus::nwords<DW>();
    enum { HDR = 0, CFG = 1, SAMP = 2, RESP = 3 };
    // Cross-firing state: `static` is what survives re-firing in an hls::task body.
    static ap_int<16> taps[NT];
    static ap_int<16> hist[NT];          // hist[0] = newest sample
    static ap_uint<DW> cbuf[CW];
    static ap_uint<DW> sbuf[SW];
    static ap_uint<2> state = HDR;
    static ap_uint<4> ci = 0;            // config words collected
    static ap_uint<4> si = 0;            // status words still to emit (0 = idle)
    static bool want_pub = false;
    static ap_uint<32> nleft = 0, npkt = 0;
    static ap_uint<16> tx_id = 0, need = 0;
    static ap_uint<32> nsamp = 0;
    static ap_uint<16> ncfg = 0;
#pragma HLS ARRAY_PARTITION variable=taps complete dim=1
#pragma HLS ARRAY_PARTITION variable=hist complete dim=1
#pragma HLS ARRAY_PARTITION variable=cbuf complete dim=1
#pragma HLS ARRAY_PARTITION variable=sbuf complete dim=1

    ap_uint<DW> w;
    if (state == HDR) {
        if (s_in.read_nb(w)) {
            ap_uint<DW> hb[1] = {w};
            FirCmdHdr h;
            h.read_array<DW>(hb);
            nleft = h.nsamp; npkt = h.nsamp; tx_id = h.tx_id; need = h.cfg_seq;
            state = (ncfg < need) ? CFG : ((h.nsamp != 0) ? SAMP : RESP);
        }
    } else if (state == CFG) {
        if (s_cfg.read_nb(w)) {
            cbuf[ci] = w;
            if (ci == CW - 1) {
                ci = 0;
                FirCfg c;
                c.read_array<DW>(cbuf);
            LOAD: for (int k = 0; k < NT; ++k) {
#pragma HLS UNROLL
                    taps[k] = (k < (int)c.ntaps) ? c.coeffs.data[k] : ap_int<16>(0);
                }
                ncfg++;
                if (ncfg >= need) state = (nleft != 0) ? SAMP : RESP;
            } else {
                ci++;
            }
        }
    } else if (state == SAMP) {
        if (s_in.read_nb(w)) {
            ap_int<16> x = w;            // one sample per word: the low 16 bits ARE the sample
            ap_int<64> acc = 0;
        MAC: for (int k = 0; k < NT; ++k) {
#pragma HLS UNROLL
                const ap_int<16> h = (k == 0) ? x : hist[k - 1];
                acc += taps[k] * h;
            }
        SHIFT: for (int k = NT - 1; k > 0; --k) {
#pragma HLS UNROLL
                hist[k] = hist[k - 1];
            }
            hist[0] = x;
            m_out.write((ap_uint<DW>)acc);
            nsamp++;
            nleft--;
            if (nleft == 0) state = RESP;
        }
    } else {                             // RESP: which packet, and the config it ACTUALLY used
        FirRespHdr r;
        r.nsamp = npkt; r.tx_id = tx_id; r.cfg_seq = ncfg;
        ap_uint<DW> rb[1];
        r.write_array<DW>(rb);
        m_resp.write(rb[0]);
        want_pub = true;
        state = HDR;
    }

    // -- status: serialize once, then one word per firing ------------------------------------
    if (si == 0 && want_pub) {
        FirStatus st;
        st.nsamp = nsamp; st.ncfg = ncfg;
        st.write_array<DW>(sbuf);
        si = SW;
        want_pub = false;
    }
    if (si != 0) {
        m_status.write(sbuf[SW - si]);
        si--;
    }
}

#endif  // EXAMPLES_MM_FIR_TASK_H
