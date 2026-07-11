#ifndef WAVEFLOW_BUILD_MEM_SEQ_TASK_H
#define WAVEFLOW_BUILD_MEM_SEQ_TASK_H
// mem_seq_task.h — the FIXED Sequencer body for the MemCopy composite (Phase 2, plans/mem_stream_impl.md).
// A pure-stream, single-firing hls::task body (the runtime re-fires it per command; NO internal command
// loop): dequeue one CopyCmd{src_off, dst_off, n_words} and issue one MRCmd{src_off, n} + one
// MWCmd{dst_off, n} — a straight copy needs no demux.  Touches ONLY streams (no m_axi), so it composes
// as an internal hls::task wired to MemRStream/MemWStream via hls_thread_local FIFOs.  Copied verbatim
// into a kernel's include dir by waveflow.build.streamutils.MemStreamStep and instantiated at a
// concrete width by the generated composite top (mem_seq_task<64>).  Word-granular (ap_uint<MEM_DW>);
// all offsets are element/word coordinates (the word_index convention — plans/component.md).
#include "hls_stream.h"
#include <ap_int.h>
#include "copy_cmd.h"
#include "m_r_cmd.h"
#include "m_w_cmd.h"

// CopyCmd -> {MRCmd, MWCmd}: element coordinates pass through verbatim (no byte<->word conversion).
template <int MEM_DW>
static void mem_seq_task(hls::stream<ap_uint<MEM_DW> >& s_cmd,
                         hls::stream<ap_uint<MEM_DW> >& mr_cmd,
                         hls::stream<ap_uint<MEM_DW> >& mw_cmd) {
    CopyCmd c;
    c.read_stream<MEM_DW>(s_cmd);
    MRCmd r;
    r.word_index = c.src_off;
    r.n_words    = c.n_words;
    r.write_stream<MEM_DW>(mr_cmd);
    MWCmd w;
    w.word_index = c.dst_off;
    w.n_words    = c.n_words;
    w.write_stream<MEM_DW>(mw_cmd);
}

#endif  // WAVEFLOW_BUILD_MEM_SEQ_TASK_H
