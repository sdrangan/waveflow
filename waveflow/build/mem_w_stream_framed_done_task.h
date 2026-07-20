#ifndef WAVEFLOW_BUILD_MEM_W_STREAM_FRAMED_DONE_TASK_H
#define WAVEFLOW_BUILD_MEM_W_STREAM_FRAMED_DONE_TASK_H
// mem_w_stream_framed_done_task.h — the FIXED in-band s2a body WITH a completion echo
// (plans/memcopy_inband_integration.md), the framed alternative to mem_w_stream_done_task.h.  There is
// NO separate command port: the single framed s_in carries [WrCmd | xfer_len payload words | len data
// words], so a descriptor can never be paired with the wrong data — that is the point of in-band
// framing.  Reads the WrCmd, buffers the opaque payload (it is echoed AFTER the write, so it must be
// held across the data phase — MAX_XFER bounds that buffer, not the protocol), pure-writes len words to
// m_mem, then echoes [WrComplete | payload] on the word s_done boundary so a host can correlate
// per-job completion.  Single-firing (the hls::task runtime re-fires per command); the sole m_axi WRITE
// owner touches ONLY streams besides m_mem.  Its pysim twin is MemWStream._run_iter_inband.  Verify via XSI.
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "wr_cmd.h"
#include "wr_complete.h"

// framed s_in -> m_mem (+ word s_done echo).  Read WrCmd (fixed words, last on its final word), buffer
// xfer_len payload words, pure-write len data words at addr (element coordinate — m_mem is a word
// pointer), then write WrComplete{len, xfer_len} + the echoed payload on the word s_done port. Word rate.
template <int MEM_DW, int MAX_XFER>
static void mem_w_stream_framed_done_task(hls::stream<streamutils::framed_word<MEM_DW> >& s_in,
                                          ap_uint<MEM_DW>* m_mem,
                                          hls::stream<ap_uint<MEM_DW> >& s_done) {
    WrCmd c;
    streamutils::tlast_status tl;
    c.read_framed_stream<MEM_DW>(s_in, tl);
    const int w0 = (int)c.addr;
    const int nw = (int)c.len;
    const int nx = (int)c.xfer_len;
    ap_uint<MEM_DW> payload[MAX_XFER];
#pragma HLS ARRAY_PARTITION variable=payload complete
    bool last;
BUFP: for (int i = 0; i < nx; ++i) {
#pragma HLS PIPELINE II=1
        payload[i] = streamutils::read_boundary_word<streamutils::framed_word<MEM_DW>, MEM_DW>(s_in, last);
    }
S2A: for (int w = 0; w < nw; ++w) {
#pragma HLS PIPELINE II=1
        m_mem[w0 + w] =
            streamutils::read_boundary_word<streamutils::framed_word<MEM_DW>, MEM_DW>(s_in, last);
    }
    WrComplete comp;
    comp.len = nw;
    comp.xfer_len = nx;
    comp.write_stream<MEM_DW>(s_done);
ECHO: for (int i = 0; i < nx; ++i) {
#pragma HLS PIPELINE II=1
        s_done.write(payload[i]);
    }
}

#endif  // WAVEFLOW_BUILD_MEM_W_STREAM_FRAMED_DONE_TASK_H
