#ifndef WAVEFLOW_BUILD_MEM_W_STREAM_FRAMED_DONE_TASK_H
#define WAVEFLOW_BUILD_MEM_W_STREAM_FRAMED_DONE_TASK_H
// mem_w_stream_framed_done_task.h — the FIXED in-band s2a body (plans/memcopy_inband_integration.md),
// the framed alternative to mem_w_stream_done_task.h.  There is NO separate command port: the single
// framed s_in carries [MemWCmd | fwd_bursts opaque bursts | len data words], so a descriptor can never
// be paired with the wrong data.  The writer reads the MemWCmd, BUFFERS the opaque forward bursts (the
// response — it must not go out before the data is stored), pure-writes len words to m_mem, then emits
// the buffered words on the word s_done boundary.  It never parses what it forwards and constructs no
// completion of its own — the composite's sequencer framed the response (a CopyResp).  MAX_XFER bounds
// the forward buffer, not the protocol.  Single-firing (the hls::task runtime re-fires per command);
// the sole m_axi WRITE owner touches ONLY streams besides m_mem.  Its pysim twin is
// MemWStream._run_iter_inband.  Verify via XSI.
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "mem_w_cmd.h"

// framed s_in -> m_mem (+ word s_done echo).  Read MemWCmd (fixed words, last on its final word), buffer
// fwd_bursts opaque bursts word-for-word, pure-write len data words at addr (element coordinate — m_mem
// is a word pointer), then emit the buffered words on the word s_done port. Word rate.
template <int MEM_DW, int MAX_XFER>
static void mem_w_stream_framed_done_task(hls::stream<streamutils::framed_word<MEM_DW> >& s_in,
                                          ap_uint<MEM_DW>* m_mem,
                                          hls::stream<ap_uint<MEM_DW> >& s_done) {
    MemWCmd c;
    streamutils::tlast_status tl;
    c.read_framed_stream<MEM_DW>(s_in, tl);
    const int w0 = (int)c.addr;
    const int nw = (int)c.len;
    const int nfwd = (int)c.fwd_bursts;
    ap_uint<MEM_DW> fwd[MAX_XFER];
#pragma HLS ARRAY_PARTITION variable=fwd complete
    bool last;
    int n_buf = 0;
    // Buffer the opaque forward bursts (the response) -- count last markers, never parse the data.
    if (nfwd > 0) {
        int seen = 0;
    BUFF: do {
#pragma HLS PIPELINE II=1
            ap_uint<MEM_DW> d =
                streamutils::read_boundary_word<streamutils::framed_word<MEM_DW>, MEM_DW>(s_in, last);
            fwd[n_buf++] = d;
            if (last) ++seen;
        } while (seen < nfwd);
    }
S2A: for (int w = 0; w < nw; ++w) {
#pragma HLS PIPELINE II=1
        m_mem[w0 + w] =
            streamutils::read_boundary_word<streamutils::framed_word<MEM_DW>, MEM_DW>(s_in, last);
    }
ECHO: for (int i = 0; i < n_buf; ++i) {
#pragma HLS PIPELINE II=1
        s_done.write(fwd[i]);
    }
}

#endif  // WAVEFLOW_BUILD_MEM_W_STREAM_FRAMED_DONE_TASK_H
