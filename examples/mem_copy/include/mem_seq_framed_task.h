#ifndef WAVEFLOW_BUILD_MEM_SEQ_FRAMED_TASK_H
#define WAVEFLOW_BUILD_MEM_SEQ_FRAMED_TASK_H
// mem_seq_framed_task.h — the FIXED in-band Sequencer body (plans/memcopy_inband_integration.md).
// The framed alternative to the GENERATED two-stream mem_seq_task.h: instead of issuing an MRCmd and
// an MWCmd on two separate command streams, it frames ONE stream to the reader —
// [FwdCmd | WrCmd | payload] per job — so the writer's command travels welded to (ahead of) its data
// and can never be paired with the wrong burst.  Hand-written (not extracted) precisely because it
// constructs descriptors and drives a framed channel, neither of which is in the extractor vocabulary;
// its pysim twin is Sequencer._run_iter_inband.  Single-firing (the hls::task runtime re-fires per
// command); touches ONLY streams (no m_axi), so it composes as an internal hls::task.  Copied verbatim
// by MemStreamStep and instantiated at a concrete width by the generated composite top.  Verify via XSI.
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "copy_cmd.h"
#include "fwd_cmd.h"
#include "wr_cmd.h"

// CopyCmd (word boundary port) -> the reader's framed command stream.  Three framed bursts:
//   FwdCmd{addr=src_off, len=n, fwd_bursts=2}  -- tells the reader to fetch n words and relay 2 bursts
//   WrCmd {addr=dst_off, len=n, xfer_len=1}    -- the writer's descriptor, relayed opaquely by reader
//   [tx_id]                                    -- the 1-word correlation payload (echoed on WrComplete)
// The FwdCmd/WrCmd descriptors each end with last=1 (write_framed_stream's default); the payload word
// carries last=1 explicitly.  No m_axi, no state: element coordinates pass through verbatim.
template <int MEM_DW>
static void mem_seq_framed_task(hls::stream<ap_uint<MEM_DW> >& s_cmd,
                                hls::stream<streamutils::framed_word<MEM_DW> >& cmd_out) {
    CopyCmd cmd;
    cmd.read_stream<MEM_DW>(s_cmd);
    FwdCmd fwd;
    fwd.addr = cmd.src_off;
    fwd.len = cmd.n_words;
    fwd.fwd_bursts = 2;
    fwd.write_framed_stream<MEM_DW>(cmd_out);
    WrCmd wr;
    wr.addr = cmd.dst_off;
    wr.len = cmd.n_words;
    wr.xfer_len = 1;
    wr.write_framed_stream<MEM_DW>(cmd_out);
    streamutils::write_boundary_word<streamutils::framed_word<MEM_DW>, MEM_DW>(
        cmd_out, (ap_uint<MEM_DW>)cmd.tx_id, true);
}

#endif  // WAVEFLOW_BUILD_MEM_SEQ_FRAMED_TASK_H
