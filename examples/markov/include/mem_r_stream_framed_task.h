#ifndef WAVEFLOW_BUILD_MEM_R_STREAM_FRAMED_TASK_H
#define WAVEFLOW_BUILD_MEM_R_STREAM_FRAMED_TASK_H
// mem_r_stream_framed_task.h — the FIXED in-band a2s body (plans/memcopy_inband_integration.md), the
// framed alternative to mem_r_stream_task.h.  Reads a FwdCmd off the framed command stream, relays the
// fwd_bursts OPAQUE bursts that follow it verbatim (the writer's WrCmd + payload — never parsed here),
// then fetches the src data and emits [relayed prefix | data] as one framed chain to the writer.
// Because it relays without decoding, the writer's descriptor rides welded to the data and cannot be
// mispaired.  Single-firing (the hls::task runtime re-fires per command); the sole m_axi READ owner
// touches ONLY streams besides m_mem.  Copied verbatim by MemStreamStep; instantiated at a concrete
// width by the generated composite top.  Its pysim twin is MemRStream._run_iter_inband.  Verify via XSI.
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "mem_r_cmd.h"

// framed s_cmd -> framed m_out.  Read MemRCmd (fixed words, last on its final word), relay fwd_bursts
// opaque bursts word-for-word with their last bits intact, then burst len src words out with last on
// the final data word.  addr is an element/word coordinate (m_mem is already a word pointer). Word rate.
template <int MEM_DW>
static void mem_r_stream_framed_task(hls::stream<streamutils::framed_word<MEM_DW> >& s_cmd,
                                     const ap_uint<MEM_DW>* m_mem,
                                     hls::stream<streamutils::framed_word<MEM_DW> >& m_out) {
    MemRCmd c;
    streamutils::tlast_status tl;
    c.read_framed_stream<MEM_DW>(s_cmd, tl);
    const int w0 = (int)c.addr;
    const int nw = (int)c.len;
    const int nfwd = (int)c.fwd_bursts;
    // Relay nfwd opaque bursts verbatim -- count last markers, never look at the data.  A single
    // pipelined loop with a data-dependent exit (proven synthesizable: experiment/hls_task relay probe).
    // The `if (nfwd > 0)` guard is REQUIRED: a bare do-while runs once even when nfwd==0, reading a
    // phantom word off s_cmd that was never meant to be relayed -- which blocks (empty s_cmd) or steals
    // the next command's first word.  A command with fwd_bursts=0 (e.g. the 2nd of two reads per job)
    // is legal and must relay nothing.  Mirrors the guard in mem_w_stream_framed_done_task.
    if (nfwd > 0) {
        int seen = 0;
    RELAY: do {
#pragma HLS PIPELINE II=1
            bool last;
            ap_uint<MEM_DW> d =
                streamutils::read_boundary_word<streamutils::framed_word<MEM_DW>, MEM_DW>(s_cmd, last);
            streamutils::write_boundary_word<streamutils::framed_word<MEM_DW>, MEM_DW>(m_out, d, last);
            if (last) ++seen;
        } while (seen < nfwd);
    }
    // Fetch nw src words, emit as one framed burst (last on the final data word).
A2S: for (int w = 0; w < nw; ++w) {
#pragma HLS PIPELINE II=1
        streamutils::write_boundary_word<streamutils::framed_word<MEM_DW>, MEM_DW>(
            m_out, m_mem[w0 + w], (w == nw - 1));
    }
}

#endif  // WAVEFLOW_BUILD_MEM_R_STREAM_FRAMED_TASK_H
