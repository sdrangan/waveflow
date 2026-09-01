#ifndef WAVEFLOW_SHOT_TX_PLAY_TASK_H
#define WAVEFLOW_SHOT_TX_PLAY_TASK_H
// shot_tx_play_task.h — one loaded shot -> `nrepeat` plays, and the ONE signal that says they are over.
//
// The shot transmitter's player (plans/rf_shot_buf.md, Stage B).  Its Python twin is
// ShotTxPlay.run_iter, which is the pysim golden and NOT the source of this file.
//
// IT SITS ON BOTH THE TOKEN CHANNEL AND THE SAMPLE PATH, AND IT NEEDS BOTH.
//
// rf_shot_buf_read_task.h plays one shot per `rdy` token and is RTL-gated as it stands, so the
// repeat has to come from in front of it.  It could have been the LOADER writing `nrepeat` tokens --
// and that is wrong for a reason worth writing down: the token channel is depth 1, so the write of
// token k+1 returns when the reader STARTS play k, not when it finishes.  A loader that cleared
// `busy` there would free the memory while the last play was still coming out of it, and the next
// load would overwrite a waveform mid-play.  Passing the samples through is what makes completion
// EXACT: `done` goes out after the last word of the last play has been handed on.
//
// THERE IS NO SCHEDULE HERE.  NONE.
//
// No slot arithmetic, no deadline, no lateness verdict, no ack.  The converter back-pressures, the
// re-layout back-pressures, this task stalls, and the memory holds -- and that IS the whole of the
// never-miss-a-deadline obligation on a shot design.  Once a play has started, a BRAM read at II=1
// can always supply a word per cycle, so the only reachable underruns are the ones before the first
// word arrives.  Compare rf_tx_player_task.h, which needs an absolute slot grid, a status channel
// and a BEFORE/MISSED verdict because ITS source can genuinely fall behind.  Same user story, and
// the difference between the two bodies is what docs/guide/rf/choosing.md is dividing on.
//
// NO `static`, ANYWHERE, AND THEREFORE NO RESET TRAP.
//
// A play-set lives entirely inside one firing, so there is no state to carry across firings.  The
// trap (reference-hls-task-reset-trap) is a task that WRITES before it READS, whose statics then
// advance during reset; this body's first act is a blocking read, twice over.  `k` and `i` are loop
// indices, which is the same reason rf_shot_buf_load_task.h needs no static either.
//
// THE OUTER LOOP'S TRIP COUNT IS DATA-DEPENDENT AND THAT COSTS NOTHING.
//
// `nrep` comes off a stream, so `k < nrep` cannot be flattened -- and it does not need to be: it is
// the OUTER loop, and the boundary between plays is paid once per shot rather than once per word
// (the same shape rf_shot_buf_load_task.h uses for the same reason).  The INNER loop is counted at
// NW with PIPELINE II=1, and that II is the number Stage B is measured on.
#include "hls_stream.h"
#include <ap_int.h>

/// @tparam W   word width in bits.
/// @tparam NW  words in one shot -- the inner loop's trip count, and the same build-time number
///             rf_shot_buf_read_task.h emits per token.  Restating it here rather than reading it
///             off a stream is deliberate: it is structure, and structure a command could contradict
///             is a second source of truth.
template <int W, int NW>
static void shot_tx_play_task(hls::stream<ap_uint<W> >& rep_in,
                              hls::stream<ap_uint<W> >& rdy_in,
                              hls::stream<ap_uint<W> >& rdy_out,
                              hls::stream<ap_uint<W> >& samp_in,
                              hls::stream<ap_uint<W> >& samp_out,
                              hls::stream<ap_uint<W> >& done_out) {
    // How many plays this shot gets.  ZERO is a real answer, not an error: it is what the loader
    // sends for a shot it refused to call playable, and the token below still has to be consumed.
    ap_uint<W> nrep = rep_in.read();
    (void)rdy_in.read();                    // blocks until the loader has filled a whole shot

    // BOTH loops are labelled, and the outer one matters as much as the inner.  Vitis names an
    // unlabelled loop `VITIS_LOOP_<line>_1` and nests that name into its children, so with only
    // `play_one:` the synthesized module came out `..._Pipeline_VITIS_LOOP_61_1_play_one` -- a name
    // that moves whenever a comment above it does, and a gate looking the II up by name then MISSES
    // and skips, which reads as a pass.
play_set:
    for (ap_uint<W> k = 0; k < nrep; k = k + 1) {
        rdy_out.write((ap_uint<W>)1);       // arm one play; the reader takes exactly one per shot
    play_one:
        for (int i = 0; i < NW; i++) {
#pragma HLS PIPELINE II=1
            samp_out.write(samp_in.read());
        }
    }

    // Only now.  Every word of every play has been handed on, so this cannot be early -- which is
    // the entire reason this task is in the sample path at all.
    done_out.write((ap_uint<W>)1);
}

#endif  // WAVEFLOW_SHOT_TX_PLAY_TASK_H
