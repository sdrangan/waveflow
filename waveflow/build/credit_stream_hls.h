#ifndef WAVEFLOW_BUILD_CREDIT_STREAM_HLS_H
#define WAVEFLOW_BUILD_CREDIT_STREAM_HLS_H
// credit_stream_hls.h -- the two ends of a credit stream in an HLS kernel body (FRAMEWORK, fixed).
//
// The C++ twins of waveflow/hw/reverse_stream.py's CreditStreamMasterIF.write and
// CreditStreamSlaveIF's batched offer, for a credit stream whose two halves are ordinary
// hls::stream ports -- direct, or routed over a bus (docs/guide/interface/axi_mm/credit_streams.md).
// The pattern they serve is "decide per chunk, pipeline inside"
// (docs/guide/interface/axi_mm/credit_streams_hls.md):
//
//     static credit::Producer<QDEPTH> crd;            // static: survives the task's firings
//     CHUNKS: for (...) {
//         crd.wait_room(m_crd, cw);                    // admit the chunk -- OUTSIDE the loop
//         LOOP: for (...) { #pragma HLS PIPELINE II=1 ... m_fwd.write(...) ... }
//         crd.sent(cw);
//     }
//
//     static credit::Consumer<CRD_EVERY> crd;
//     CHUNKS: for (...) {
//         LOOP: for (...) { ... s_fwd.read() ... crd.took(1) ... }
//         crd.offer(s_crd);                            // report -- OUTSIDE the loop
//     }
//
// Counters are CTR_BITS = 16 wide and compared modulo 2^16 (ap_uint wraps by itself): exact while a
// difference stays below 2^16, which the window guarantees.  Credit values are CUMULATIVE words
// consumed, so a lost or overwritten value is harmless -- the next carries the whole truth.
#include "hls_stream.h"
#include <ap_int.h>

namespace credit {

static const int CTR_BITS = 16;
typedef ap_uint<CTR_BITS> ctr_t;

/// The producer end.  DEPTH is the consumer's queue in words; RESP_WORDS of it are reserved (as
/// CreditStreamMasterIF.resp_words).  A write -- one chunk -- must be no longer than max_write =
/// DEPTH - RESP_WORDS - (CRD_EVERY - 1) words for the consumer's batching, or a wait may never end;
/// check it with a static_assert where CRD_EVERY is known.
template <int DEPTH, int RESP_WORDS = 1>
struct Producer {
    ctr_t written = 0;   ///< cumulative words written
    ctr_t acked = 0;     ///< the newest cumulative count the consumer reported

    /// Words the window still has room for.
    int room() const { return (DEPTH - RESP_WORDS) - (int)ctr_t(written - acked); }

    /// Return once *nwords* fit: first take any counts already waiting (a bounded drain, so the
    /// credit FIFO never fills with stale values), then, while they do not fit, sleep on the credit
    /// stream for the next one and catch up to the newest.  It never polls: a blocking read waits for
    /// an arrival.  Call it BEFORE a pipelined loop, never inside one.
    template <int DW>
    void wait_room(hls::stream<ap_uint<DW> >& crd, int nwords) {
        ap_uint<DW> v;
    DRAIN0: for (int i = 0; i < 4; ++i) {
            if (!crd.read_nb(v)) break;
            acked = v(CTR_BITS - 1, 0);
        }
    WAIT: while (room() < nwords) {
            acked = crd.read()(CTR_BITS - 1, 0);
        DRAIN: for (int i = 0; i < 4; ++i) {
                if (!crd.read_nb(v)) break;
                acked = v(CTR_BITS - 1, 0);
            }
        }
    }

    /// Record *nwords* written (after the write -- the chunk -- that wait_room admitted).
    void sent(int nwords) { written += nwords; }
};

/// The consumer end: counts what it consumes and reports the cumulative total once CRD_EVERY words
/// are unreported.
template <int CRD_EVERY>
struct Consumer {
    ctr_t consumed = 0;  ///< cumulative words consumed
    ctr_t offered = 0;   ///< the count last reported

    /// Record *nwords* taken off the forward stream.  Cheap enough for a pipelined loop.
    void took(int nwords) { consumed += nwords; }

    /// Report the count if CRD_EVERY or more words are unreported.  A blocking write: what reads a
    /// credit stream (the producer, or a credit bus writer that coalesces and never waits) always
    /// drains it -- and a dropped report could leave a producer waiting for exactly that credit with
    /// nothing more arriving to prompt another.  Call it BETWEEN chunks.
    template <int DW>
    void offer(hls::stream<ap_uint<DW> >& crd) {
        if (ctr_t(consumed - offered) >= CRD_EVERY) {
            crd.write(ap_uint<DW>(consumed));
            offered = consumed;
        }
    }
};

}  // namespace credit

#endif  // WAVEFLOW_BUILD_CREDIT_STREAM_HLS_H
