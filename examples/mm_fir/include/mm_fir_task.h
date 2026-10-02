#ifndef EXAMPLES_MM_FIR_TASK_H
#define EXAMPLES_MM_FIR_TASK_H
// mm_fir_task.h -- the mm_fir kernel body (HAND-WRITTEN), the HLS twin of MmFir.run_iter.
//
// plans/mm_slave_adaptor.md, the witness example.  A free-running hls::task body, pipelined at II=1:
// ONE firing per cycle, and every firing moves AT MOST ONE WORD on each stream -- that is what lets
// the whole body pipeline.  (The first version read a whole 5-word FirCfg and wrote a whole 2-word
// FirStatus inside one firing; the body could not pipeline and the kernel ran at ~1 sample per 10
// cycles, measured in XSI.)  Per firing:
//
//   * input  -- a CONFIG word if s_cfg has one (config first), else a SAMPLE if s_in has one.
//     Config words collect in cbuf; on the last one the generated FirCfg::read_array decodes it
//     (nothing here unpacks a word), it is counted, and parked in the single pending slot.
//     A sample puts a pending config in force if its apply_at has come, and is filtered.
//   * output -- at most one result word on m_out (from a sample) and at most one status word on
//     m_status.
//
// Status: a FirStatus is published after every config and on the first idle firing after samples.
// It is serialized once (FirStatus::write_array) and emitted a word per firing; a publish requested
// while one is going out waits for it to finish -- restarting would hand the status bank half of one
// message and half of the next.
//
// A config whose apply_at has already passed is counted `late` and put in force from the next sample:
// detected, never silently misapplied.  A config arriving while another is pending first puts the
// pending one in force (one slot, as the pysim twin).
//
// Numbers: int16 samples (the low 16 bits of each 64-bit word), int16 taps, the exact integer sum in
// an ap_int<64>, written as its two's complement word.  No rounding, so pysim's numpy golden is
// bit-exact by construction.
#include "hls_stream.h"
#include <ap_int.h>
#include "fir_cfg.h"
#include "fir_status.h"

template <int DW>
static void mm_fir_task(hls::stream<ap_uint<DW> >& s_cfg,
                        hls::stream<ap_uint<DW> >& s_in,
                        hls::stream<ap_uint<DW> >& m_out,
                        hls::stream<ap_uint<DW> >& m_status) {
#pragma HLS PIPELINE II=1
    const int NT = 16;
    const int CW = FirCfg::nwords<DW>();
    const int SW = FirStatus::nwords<DW>();
    // Cross-firing state: `static` is what survives re-firing in an hls::task body.
    static ap_int<16> taps[NT];
    static ap_int<16> ptaps[NT];
    static ap_int<16> hist[NT];          // hist[0] = newest sample
    static ap_uint<DW> cbuf[CW];
    static ap_uint<DW> sbuf[SW];
    static ap_uint<4> ci = 0;            // config words collected
    static ap_uint<4> si = 0;            // status words still to emit (0 = idle)
    static bool pvalid = false, dirty = false, want_pub = false;
    static ap_uint<32> pat = 0;
    static ap_uint<32> nsamp = 0, ncfg = 0, late = 0;
#pragma HLS ARRAY_PARTITION variable=taps complete dim=1
#pragma HLS ARRAY_PARTITION variable=ptaps complete dim=1
#pragma HLS ARRAY_PARTITION variable=hist complete dim=1
#pragma HLS ARRAY_PARTITION variable=cbuf complete dim=1
#pragma HLS ARRAY_PARTITION variable=sbuf complete dim=1

    ap_uint<DW> w;
    if (s_cfg.read_nb(w)) {
        cbuf[ci] = w;
        if (ci == CW - 1) {
            ci = 0;
            FirCfg c;
            c.read_array<DW>(cbuf);
            ncfg++;
            ap_uint<32> at = c.apply_at;
            if (at < nsamp) { late++; at = nsamp; }
        STAGE: for (int k = 0; k < NT; ++k) {
#pragma HLS UNROLL
                if (pvalid) taps[k] = ptaps[k];          // superseded: in force now
                ptaps[k] = (k < (int)c.ntaps) ? c.coeffs.data[k] : ap_int<16>(0);
            }
            pvalid = true;
            pat = at;
            want_pub = true;
        } else {
            ci++;
        }
    } else if (s_in.read_nb(w)) {
        ap_int<16> x = w;                // one sample per word: the low 16 bits ARE the sample
        const bool apply = pvalid && pat <= nsamp;
        ap_int<64> acc = 0;
    MAC: for (int k = 0; k < NT; ++k) {
#pragma HLS UNROLL
            const ap_int<16> t = apply ? ptaps[k] : taps[k];
            const ap_int<16> h = (k == 0) ? x : hist[k - 1];
            acc += t * h;
        }
        if (apply) {
        APPLY: for (int k = 0; k < NT; ++k) {
#pragma HLS UNROLL
                taps[k] = ptaps[k];
            }
            pvalid = false;
        }
    SHIFT: for (int k = NT - 1; k > 0; --k) {
#pragma HLS UNROLL
            hist[k] = hist[k - 1];
        }
        hist[0] = x;
        m_out.write((ap_uint<DW>)acc);
        nsamp++;
        dirty = true;
    } else if (dirty) {
        want_pub = true;
        dirty = false;
    }

    // -- status: serialize once, then one word per firing ------------------------------------
    if (si == 0 && want_pub) {
        FirStatus st;
        st.nsamp = nsamp; st.ncfg = ncfg; st.late = late;
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
