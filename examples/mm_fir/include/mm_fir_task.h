#ifndef EXAMPLES_MM_FIR_TASK_H
#define EXAMPLES_MM_FIR_TASK_H
// mm_fir_task.h -- the mm_fir kernel body (HAND-WRITTEN), the HLS twin of MmFir.run_iter.
//
// plans/mm_fir_cfg_seq.md: the stream_inband pattern with a config sequence number.  ONE FIRING = ONE
// PACKET, written as the Python reads -- straight-line, a loop over the samples:
//
//   1. read the packet's FirCmdHdr (nsamp, tx_id, cfg_seq) off s_in;
//   2. take configs off s_cfg until cfg_seq of them have arrived -- WAITING for one that has not (a
//      config nobody has asked for stays in s_cfg);
//   3. filter nsamp samples, ONE PER CYCLE: the lane loop at one sample per cycle
//      (docs/guide/vectorization/hls/loop_optimization.md).  Samples are int16, four to a word; a word
//      is read and unpacked into a lane every fourth iteration, and each iteration uses one sample of
//      the lane -- the 16-tap MAC is not replicated four times;
//   4. publish the status (nsamp, ncfg), THEN the response: which packet, and which config it was
//      filtered with.  Status first, so a host holding a response knows the status already counts the
//      packet (plans/mm_irq.md D4).
//
// The order every read and write happens in is fixed by the packet, so blocking reads cannot deadlock.
// The taps, the filter's history and the counters are `static`: they survive from firing to firing
// (the hls::task runtime re-fires the body per packet).
//
// Numbers: int16 samples, int16 taps, the exact integer sum in an ap_int<64>.  Nothing here packs or
// unpacks a word by hand: the generated FirCfg / FirCmdHdr / FirRespHdr / FirStatus structs and the
// int16 / int64 lane routines do.  No rounding, so pysim's numpy golden is bit-exact by construction.
#include "hls_stream.h"
#include <ap_int.h>
#include "fir_cfg.h"
#include "fir_cmd_hdr.h"
#include "fir_resp_hdr.h"
#include "fir_status.h"
#include "int16_array_utils.h"
#include "int64_array_utils.h"

template <int DW>
static void mm_fir_task(hls::stream<ap_uint<DW> >& s_cfg,
                        hls::stream<ap_uint<DW> >& s_in,
                        hls::stream<ap_uint<DW> >& m_out,
                        hls::stream<ap_uint<DW> >& m_resp,
                        hls::stream<ap_uint<DW> >& m_status) {
    const int NT = 16;
    const int PF = int16_array_utils::lane_capacity<DW>();      // samples per input word
    static ap_int<16> taps[NT];
    static ap_int<16> hist[NT];          // hist[0] = newest sample
    static ap_uint<32> nsamp = 0;        // samples filtered so far
    static ap_uint<16> ncfg = 0;         // configs taken so far
#pragma HLS ARRAY_PARTITION variable=taps complete dim=1
#pragma HLS ARRAY_PARTITION variable=hist complete dim=1

    // 1. the header
    FirCmdHdr h;
    h.read_stream<DW>(s_in);

    // 2. the config this packet needs
CFG: while (ncfg < h.cfg_seq) {
        FirCfg c;
        c.read_stream<DW>(s_cfg);
    LOAD: for (int k = 0; k < NT; ++k) {
#pragma HLS UNROLL
            taps[k] = (k < (int)c.ntaps) ? c.coeffs.data[k] : ap_int<16>(0);
        }
        ncfg++;
    }

    // 3. the samples, one per cycle; a word every PF of them
    int16_array_utils::value_type x_lane[PF];
#pragma HLS ARRAY_PARTITION variable=x_lane complete dim=1
SAMP: for (ap_uint<32> i = 0; i < h.nsamp; ++i) {
#pragma HLS PIPELINE II=1
        const int k = (int)(i % PF);
        if (k == 0) {                    // a fresh word: unpack it into the lane
            const ap_uint<32> nrem = h.nsamp - i;
            ap_uint<DW> w = s_in.read();
            int16_array_utils::read_array_lane<DW>(&w, x_lane, (nrem < PF) ? (int)nrem : PF);
        }
        const ap_int<16> x = x_lane[k];
        ap_int<64> acc = 0;
    MAC: for (int t = 0; t < NT; ++t) {
#pragma HLS UNROLL
            acc += taps[t] * ((t == 0) ? x : hist[t - 1]);
        }
    SHIFT: for (int t = NT - 1; t > 0; --t) {
#pragma HLS UNROLL
            hist[t] = hist[t - 1];
        }
        hist[0] = x;
        int64_array_utils::value_type y_lane[1] = {acc};
        ap_uint<DW> yw;
        int64_array_utils::write_array_lane<DW>(y_lane, &yw, 1);
        m_out.write(yw);
    }
    nsamp += h.nsamp;

    // 4. the status, then the response
    FirStatus st;
    st.nsamp = nsamp; st.ncfg = ncfg;
    st.write_stream<DW>(m_status);
    FirRespHdr r;
    r.nsamp = h.nsamp; r.tx_id = h.tx_id; r.cfg_seq = ncfg;
    r.write_stream<DW>(m_resp);
}

#endif  // EXAMPLES_MM_FIR_TASK_H
