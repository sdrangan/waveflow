#ifndef WAVEFLOW_RF_SHOT_BUF_READ_TASK_H
#define WAVEFLOW_RF_SHOT_BUF_READ_TASK_H
// rf_shot_buf_read_task.h — wait for a shot, then emit it, in order.
//
// The finite buffer's reader (plans/rf_shot_buf.md, Stage A).  Its Python twin is
// RfShotBufRead.run_iter, which is the pysim golden and NOT the source of this file.
//
// THERE IS NO ADDRESS STREAM, AND THAT IS THE SIMPLIFICATION.
//
// examples/bram_simple's reader answers a (rp, nwords) command, which makes it a witness for the memory
// rather than a buffer.  A shot buffer plays a CONTIGUOUS shot, so the address is the loop index and
// the only thing crossing the boundary is payload.  Nothing on this wire arbitrates, because there
// is nothing to arbitrate: the writer is not live while this runs.
//
// THE `rdy` READ IS THE ONLY BLOCKING POINT, AND IT IS OUTSIDE THE PIPELINE.
//
// The arming used to be spelled `if (!armed) rdy.read();` inside the per-word loop (that is what
// bram_read_task.h does, because its reader is address-driven and has no shot structure).  Hoisting
// it to the outer loop is not a micro-optimisation: a conditional blocking read inside a pipelined
// body is a data-dependent stall, which is the shape Vitis reports as
//
//     [HLS 200-878] Unable to schedule the loop exit test ... (II = 1)
//
// and which pins the streaming buffer's two blocking bodies at II=2.  The shot design can hoist it
// where the streaming design cannot, because the question it asks is about the WHOLE shot ("has it
// been loaded?") rather than about this word ("has this one been written yet?").
//
// NOTE the loader-hoist reversal (commit a2f93e0), where exactly this kind of hoist reached II=1 in
// csynth and played 0xFFFF for 9984 samples at RTL while every counter reported success.  That is
// why this body is gated on an RTL run and not on a report: see tests/examples/test_rf_shot_buf_xsi.py.
// The difference from that case is structural rather than hopeful -- there the hoist changed WHICH
// question was asked (frame instead of word) and deleted a case; here the question was always about
// the whole shot.
#include "hls_stream.h"
#include <ap_int.h>

/// @tparam W   word width in bits.
/// @tparam N   buffer depth in WORDS -- the ARRAY SIZE (see rf_shot_buf_load_task.h).
/// @tparam NW  words in one shot (NW <= N).
template <int W, int N, int NW>
static void rf_shot_buf_read_task(ap_uint<W> buf_r[N], hls::stream<ap_uint<W> >& rdy_in,
                                  hls::stream<ap_uint<W> >& s_out) {
    while (1) {
        (void)rdy_in.read();              // blocks until the loader has filled a whole shot
    play_shot:
        for (int i = 0; i < NW; i++) {
#pragma HLS PIPELINE II=1
            s_out.write(buf_r[i]);
        }
    }
}

#endif  // WAVEFLOW_RF_SHOT_BUF_READ_TASK_H
