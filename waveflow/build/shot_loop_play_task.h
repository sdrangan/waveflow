#ifndef WAVEFLOW_SHOT_LOOP_PLAY_TASK_H
#define WAVEFLOW_SHOT_LOOP_PLAY_TASK_H
// shot_loop_play_task.h — play the region forever, YIELD IT ON REQUEST, play filler until it returns.
//
// The infinite-play transmitter's player (plans/t2p_lock_chan.md S1), and the OWNER side of the
// lock.  Its Python twin is ShotLoopPlay.run_iter, which is the pysim golden and NOT the source of
// this file.
//
// SET THE STATE BEFORE YOU GRANT.  ALWAYS.  THIS IS THE ONE ORDERING EVERYTHING TURNS ON.
//
// `playing = 0` comes BEFORE mem_lock_grant().  Granting while this task is still reading lets the
// loader write memory it is reading -- precisely the collision the lock exists to prevent.
// bram_t2p.v's $error catches it at RTL and XSI THROWS $error AWAY
// (reference-xsi-discards-rtl-text), so nothing here would say a word.  pysim's LockedMemSlaveIF
// takes the region out of the owner's hands inside grant() and raises on the very next access, which
// is why the PYSIM gate is what proves this ordering and the waveform is not.
//
// THE POLL SITS OUTSIDE THE PIPELINED LOOP, AND THAT IS WHY II=1 SURVIVES HAVING A LOCK AT ALL.
//
// One poll per BW elements: the datapath is a counted loop with nothing data-dependent in its trip
// count, and the lock traffic is the outer loop's.  That is also the definition of `check_period` --
// the maximum elements of its own work an owner may do between polls -- so the loader's blocking
// wait for a grant is a STATED NUMBER and a gate can assert it.
//
// THE FILLER IS A VALUE, NOT A STALL.
//
// The owner CANNOT STOP.  A body that blocked while yielded would back-pressure the converter, which
// is not an option and is the entire reason this side is the owner rather than the requester.  Zero
// is the one sample a DAC can be handed that means NOTHING, so the gap is silence rather than noise.
//
// THIS BODY WRITES BEFORE IT READS, AND THEREFORE THE RESET TRAP APPLIES.
//
// reference-hls-task-reset-trap: an hls::task that WRITES before it READS advances during reset, so
// its statics move before the design is alive.  An owner cannot avoid that shape -- writing without
// being asked is what "the side that cannot stop" MEANS -- so both statics carry `#pragma HLS reset`
// AND the build needs `config_rtl -reset state` in its solution tcl, which is what actually closed
// it under Vitis 2025.1 when the pragma alone did not (measured in rf_repeat_play).  The LOADER next
// door opens with a blocking read and inherits none of this.
//
// `rd` IS A RUNNING BASE ON TOP OF A BUILD-TIME ONE.
//
// `buf[BASE + rd + i]` is the dynamic base addressing plans/t2p_lock_chan.md names as the
// silent-failure class this repo has already paid for once: consistently mis-scaled addressing
// round-trips perfectly right up to the point its memory wraps.  A body indexing `buf[i]` would
// synthesize just as well and measure nothing.
#include "hls_stream.h"
#include <ap_int.h>
#include "mem_lock.h"

//: What goes out while this task does not own the region.  See the header note -- a value, not a
//: stall.  Matches waveflow.hw.rf_shot_loop.FILLER.
#ifndef SHOT_LOOP_FILLER
#define SHOT_LOOP_FILLER 0
#endif

/// @tparam W     word width in bits -- the memory's and the output stream's.
/// @tparam D     memory depth in elements, and the bound mem_lock_grant refuses a region against.
/// @tparam NW    words in one shot -- the region's length and the wrap point of the read pointer.
/// @tparam BASE  first element of the region.  ONE number, shared with the loader: a loader and a
///               player that disagreed about where the waveform is would each be individually
///               correct.
/// @tparam BW    words per chunk: the pipelined loop's trip count AND the poll period.  Must divide
///               NW, so a chunk never straddles the end of the region -- one that did would need two
///               base additions and the play boundary would stop landing on a block boundary.
template <int W, int D, int NW, int BASE, int BW>
static void shot_loop_play_task(ap_uint<W> buf[D],
                                memlock::chan& cmd_in,
                                memlock::chan& resp_out,
                                hls::stream<ap_uint<W> >& samp_out) {
    //: 1 while this task owns the region.  STARTS AT ZERO: nothing has been loaded yet, and playing
    //: a memory that was never written is a plausible sample rather than a silence -- X or stale
    //: data at RTL, and indistinguishable from a quiet signal either way.
    static ap_uint<1> playing = 0;
#pragma HLS reset variable=playing
    //: The read pointer WITHIN the region.
    static ap_uint<32> rd = 0;
#pragma HLS reset variable=rd

    // LABELLED, and the II gate looks this module up by that label -- see the note in
    // shot_loop_load_task.h.
play_chunk:
    for (int i = 0; i < BW; i++) {
#pragma HLS PIPELINE II=1
        samp_out.write(playing ? buf[BASE + rd + i] : (ap_uint<W>)SHOT_LOOP_FILLER);
    }
    if (playing) {
        rd = rd + BW;
        if (rd >= (ap_uint<32>)NW) {
            rd = 0;
        }
    }

    MemLockCmd c;
    if (memlock::mem_lock_poll(cmd_in, c)) {
        if (c.opcode == LOCK_ACQUIRE) {
            playing = 0;                    // STOP TOUCHING IT ...
            memlock::mem_lock_grant<D>(resp_out, c.start_addr, c.end_addr);   // ... THEN grant
        } else {
            // A new waveform starts at its beginning: resuming mid-shot would splice the tail of the
            // old waveform's phase onto the new one, which is right in no application and is
            // invisible from a word count.  RELEASE is not answered -- there is nothing to race.
            rd = 0;
            playing = 1;
        }
    }
}

#endif  // WAVEFLOW_SHOT_LOOP_PLAY_TASK_H
