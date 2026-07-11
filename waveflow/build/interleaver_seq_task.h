#ifndef WAVEFLOW_BUILD_INTERLEAVER_SEQ_TASK_H
#define WAVEFLOW_BUILD_INTERLEAVER_SEQ_TASK_H
// interleaver_seq_task.h — the FIXED Sequencer body for the full interleaver (Phase 4,
// plans/mem_stream_impl.md).  A pure-stream, single-firing hls::task body (the runtime re-fires it
// per app command): dequeue one InterleaverCmd{p_off, x_off, y_off, n} and decompose it into TWO
// MRCmds (P then X, same word count) for the read-owner MemRStream + one MWCmd (Y) for the
// write-owner MemWStream.  The split word count NW = n/LW is a compile-time template arg baked from
// the one generate() job-size param (the single source of truth the Demux/Fill/Gather also bake — no
// runtime count stream, no TLAST).  Word-granular; all offsets are element/word coordinates (the
// word_index convention).  Copied verbatim by MemStreamStep, instantiated <MEM_DW, NW> by the top.
#include "hls_stream.h"
#include <ap_int.h>
#include "il_cmd.h"
#include "m_r_cmd.h"
#include "m_w_cmd.h"

// InterleaverCmd -> MRCmd(p_off,NW), MRCmd(x_off,NW), MWCmd(y_off,NW). Element coords pass through.
template <int MEM_DW, int NW>
static void interleaver_seq_task(hls::stream<ap_uint<MEM_DW> >& s_cmd,
                                 hls::stream<ap_uint<MEM_DW> >& mr_cmd,
                                 hls::stream<ap_uint<MEM_DW> >& mw_cmd) {
    InterleaverCmd c;
    c.read_stream<MEM_DW>(s_cmd);
    MRCmd rp;
    rp.word_index = c.p_off;
    rp.n_words    = NW;
    rp.write_stream<MEM_DW>(mr_cmd);       // P first
    MRCmd rx;
    rx.word_index = c.x_off;
    rx.n_words    = NW;
    rx.write_stream<MEM_DW>(mr_cmd);       // then X
    MWCmd wy;
    wy.word_index = c.y_off;
    wy.n_words    = NW;
    wy.write_stream<MEM_DW>(mw_cmd);
}

#endif  // WAVEFLOW_BUILD_INTERLEAVER_SEQ_TASK_H
