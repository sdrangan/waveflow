#ifndef WAVEFLOW_BUILD_MEM_SEQ_FRAMED_TASK_H
#define WAVEFLOW_BUILD_MEM_SEQ_FRAMED_TASK_H
// mem_seq_framed_task.h — the FIXED in-band Sequencer body (plans/memcopy_inband_integration.md).
// The framed alternative to the GENERATED two-stream mem_seq_task.h: instead of issuing an MRCmd and
// an MWCmd on two separate command streams, it frames ONE stream to the chain —
// [MemRCmd | MemWCmd | CopyResp] per job.  Each descriptor's fwd_bursts says how many following bursts
// that stage relays (MemRCmd.fwd_bursts=2: relay MemWCmd + CopyResp; MemWCmd.fwd_bursts=1: relay the
// CopyResp), so the mem-streams forward opaquely and this is the ONLY schema-aware stage.  Hand-written
// (not extracted) because it constructs descriptors and drives a framed channel, neither in the
// extractor vocabulary; its pysim twin is Sequencer.run_iter.  Single-firing (the hls::task runtime
// re-fires per command); touches ONLY streams (no m_axi).  Copied verbatim by MemStreamStep and
// instantiated at a concrete width by the generated composite top.  Verify via XSI.
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "copy_cmd.h"
#include "mem_r_cmd.h"
#include "mem_w_cmd.h"
#include "copy_resp.h"

// CopyCmd (word boundary port) -> the chain's framed command stream.  Three framed bursts:
//   MemRCmd{addr=src_off, len=n, fwd_bursts=2}  -- reader fetches n words, relays the next 2 bursts
//   MemWCmd{addr=dst_off, len=n, fwd_bursts=1}  -- writer writes n words, relays the next 1 burst
//   CopyResp{tx_id}                             -- the typed response, forwarded to s_done
// write_framed_stream ends each descriptor with last=1.  No m_axi, no state: element coordinates and
// the tx_id pass through verbatim.
template <int MEM_DW>
static void mem_seq_framed_task(hls::stream<ap_uint<MEM_DW> >& s_cmd,
                                hls::stream<streamutils::framed_word<MEM_DW> >& cmd_out) {
    CopyCmd cmd;
    cmd.read_stream<MEM_DW>(s_cmd);
    MemRCmd memr;
    memr.addr = cmd.src_off;
    memr.len = cmd.n_words;
    memr.fwd_bursts = 2;
    memr.write_framed_stream<MEM_DW>(cmd_out);
    MemWCmd memw;
    memw.addr = cmd.dst_off;
    memw.len = cmd.n_words;
    memw.fwd_bursts = 1;
    memw.write_framed_stream<MEM_DW>(cmd_out);
    CopyResp resp;
    resp.tx_id = cmd.tx_id;
    resp.write_framed_stream<MEM_DW>(cmd_out);
}

#endif  // WAVEFLOW_BUILD_MEM_SEQ_FRAMED_TASK_H
