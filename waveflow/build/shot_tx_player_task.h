#ifndef WAVEFLOW_SHOT_TX_PLAYER_TASK_H
#define WAVEFLOW_SHOT_TX_PLAYER_TASK_H
// shot_tx_player_task.h — play the region a counted number of times, or forever, and yield it.
//
// The shot transmitter's player (plans/rf_shot_unify.md), and the OWNER side of the
// lock.  Its Python twin is ShotTxPlayer.run_iter, which is the pysim golden and NOT the source of
// this file.
//
// THE MERGE IS THE FOUR LINES AFTER THE WRAP, AND NOTHING ELSE.
//
//     if (rd >= D) { rd = 0; if (!loop) { if (--nrep_left == 0) { playing = 0; done; } } }
//
// The infinite predecessor's body wrapped and kept going; the finite one counted passes in an
// outer loop and returned.  Here one body does both, and the difference is a single register read.
// Everything
// above it -- the chunk, the filler, the poll, the ordering -- is shared, which is what makes this a
// merge rather than two designs sharing a file.
//
// `rd` IS EITHER A POSITION OR A TIMESTAMP, AND `ABS` IS WHICH (plans/rf_shot_absolute.md).
//
// At ABS=0 -- the default, and every predecessor's behaviour -- `rd` is reset on accept and advances
// only while playing, so it says HOW FAR INTO THIS WAVEFORM.  At ABS=1 it advances and wraps on every
// firing, filler included, so it says HOW MANY WORDS SINCE RESET, modulo D; a shot is armed in
// `pending` at accept and starts at the next rd == 0.  Sample j then comes out at an absolute index
// congruent to j, which is the whole feature, and a waveform still starts at its beginning.
//
// THE SPLIT AT THE WRAP IS LOAD-BEARING AND FAILS SILENTLY IF YOU GET IT WRONG.  The advance leaves
// the `playing` guard; `nrep_left` and the `done` do NOT.  Move the whole block and the repeat count
// ticks on filler wraps -- a finite shot ends early or never starts -- while every word count still
// adds up.
//
// SET THE STATE BEFORE YOU GRANT.  ALWAYS.  THIS IS THE ONE ORDERING EVERYTHING TURNS ON.
//
// `playing = 0` comes BEFORE mem_lock_grant().  Granting while still reading lets the loader write
// memory this task is reading -- precisely the collision the lock exists to prevent.  bram_t2p.v's
// $error catches it at RTL and XSI THROWS $error AWAY (reference-xsi-discards-rtl-text), so nothing
// here would say a word.  pysim's LockedMemSlaveIF takes the region out of the owner's hands inside
// grant() and raises on the very next access, which is why the PYSIM gate proves this ordering and
// the waveform does not.
//
// THE PLAY COMMAND IS READ ON THE RELEASE BRANCH, AND THE BLOCKING READ THERE IS SAFE.
//
// It is CONTROL-DEPENDENT on the poll's result, so nothing can hoist it above the loader's writes --
// which is the shape that made S1's request/response deadlock (a write and a blocking read on two
// streams with no dependency, scheduled into one stalling state).  The loader writes the command
// before the release, so this costs at most a beat, inside a gap the design is already in.
//
// `done` IS OWED ONLY ON THE FINITE PATH.
//
// pc.opcode says who is waiting.  A spurious token would clear a `busy` that a LATER finite shot set
// and the next load would preempt it -- the truncation SHOT_BUSY exists to prevent, arrived at from
// the other side.  A short finite shot (nrepeat == 0) is owed one IMMEDIATELY: nothing else will
// ever send it, and without it the loader stays busy forever.
//
// THIS BODY WRITES BEFORE IT READS, AND THEREFORE THE RESET TRAP APPLIES.
//
// reference-hls-task-reset-trap: an hls::task that WRITES before it READS advances during reset.  An
// owner cannot avoid that shape -- writing without being asked is what "the side that cannot stop"
// MEANS -- so every static carries `#pragma HLS reset` AND the build needs `config_rtl -reset state`,
// which is what actually closed it under Vitis 2025.1.  The LOADER opens with a blocking read and
// inherits none of this.
#include "hls_stream.h"
#include <ap_int.h>
#include "mem_lock.h"
#include "shot_play_cmd.h"

//: What goes out while this task is not playing -- between shots, during a handover, and after a
//: finite play-set has finished.  A VALUE, not a stall: the owner cannot stop.  Matches
//: waveflow.hw.rf_shot_tx.FILLER.
#ifndef SHOT_TX_FILLER
#define SHOT_TX_FILLER 0
#endif

//: ShotTxHdr.opcode, as forwarded on the play command.  Guarded because a build may ship the
//: predecessors' bodies into the same directory.
#ifndef SHOT_OP_LOAD
#define SHOT_OP_LOAD 0
#define SHOT_OP_END  1
#endif
#ifndef SHOT_OP_LOOP
#define SHOT_OP_LOOP 2
#endif

/// @tparam W     word width in bits -- the memory's and the output stream's.
/// @tparam D     memory depth in elements, the bound mem_lock_grant refuses a region against, AND
///               the wrap point of the read pointer: THE SHOT IS THE BUFFER
///               (plans/rf_shot_geometry.md).  A power of two, so the wrap is a mask.
///
///               There is no NW and no BASE.  `buf[rd + i]` reads from the memory's own origin, so
///               the loader and the player cannot disagree about where the waveform is -- there is
///               nowhere else it could be.
/// @tparam BW    words per chunk: the pipelined loop's trip count AND the poll period.  Must divide
///               D, so a chunk never straddles the wrap and the play boundary keeps landing on a
///               block boundary.
/// @tparam ABS   THE INDEX IS A TIMESTAMP (plans/rf_shot_absolute.md).  0 is every predecessor's
///               behaviour: a shot starts at rd == 0 the instant it is accepted.  1 makes `rd`
///               advance UNCONDITIONALLY -- filler included -- so it holds this task's own word
///               count since reset modulo D, and a playout is deferred to the next rd == 0.  Then
///               sample j is always emitted at an absolute index congruent to j, which is the whole
///               feature.
///
///               ONE BODY, NOT TWO.  It is a template argument and the branches are `if (ABS)`, so
///               Vitis folds the constant and each build synthesizes exactly the design it asked
///               for.  Two copied bodies would be two designs that drift.
template <int W, int D, int BW, int ABS>
static void shot_tx_player_task(ap_uint<W> buf[D],
                                memlock::chan& cmd_in,
                                memlock::chan& resp_out,
                                hls::stream<ap_uint<W> >& rep_in,
                                hls::stream<ap_uint<W> >& done_out,
                                hls::stream<ap_uint<W> >& samp_out) {
    //: 1 while this task owns the region AND has something to play.  STARTS AT ZERO: nothing has
    //: been loaded yet, and playing a memory that was never written is a plausible sample rather
    //: than a silence -- X or stale data at RTL, indistinguishable from a quiet signal either way.
    static ap_uint<1> playing = 0;
#pragma HLS reset variable=playing
    //: 1 when the current shot is a SHOT_LOOP.  THE EXIT CONDITION, and the only difference between
    //: the two predecessors' players.
    static ap_uint<1> loop = 0;
#pragma HLS reset variable=loop
    //: The read pointer into the buffer.  It wraps at D, and that wrap is the whole of this
    //: design's addressing.
    static ap_uint<32> rd = 0;
#pragma HLS reset variable=rd
    //: Passes left on a finite shot.  Meaningless while `loop`.
    static ap_uint<32> nrep_left = 0;
#pragma HLS reset variable=nrep_left
    //: ARMED, but waiting for the boundary.  Meaningful only when ABS.  Set at accept, cleared by
    //: the rd == 0 that turns `playing` on.  BW divides D, so rd takes exactly 0, BW, ... D-BW and
    //: rd == 0 happens exactly once per pass: an exact test with no crossed-zero case.
    static ap_uint<1> pending = 0;
#pragma HLS reset variable=pending

    // THE START, AND IT IS BEFORE THE WRITE LOOP.  Testing rd == 0 here is what makes the chunk
    // starting at 0 the FIRST ONE PLAYED rather than the one after it.
    if (ABS) {
        if (pending && rd == 0) {
            playing = 1;
            pending = 0;
        }
    }

    // LABELLED, and the II gate looks this module up by that label -- see the note in
    // shot_tx_loader_task.h.
play_chunk:
    for (int i = 0; i < BW; i++) {
#pragma HLS PIPELINE II=1
        samp_out.write(playing ? buf[rd + i] : (ap_uint<W>)SHOT_TX_FILLER);
    }

    // THE ADVANCE, AND THE SPLIT plans/rf_shot_absolute.md NAMES AS THE SILENT TRAP.
    //
    // When ABS the pointer advances and wraps on EVERY firing, filler included -- that is what makes
    // it this task's own word count since reset and therefore a timestamp.  What must NOT come out
    // of the guard with it is the repeat count: `nrep_left` would then tick once per pass while the
    // design plays filler, and a finite shot would end early or never start at all.  The word counts
    // still add up either way, so no counter gate catches it.  Two conditions where there was one:
    // the advance is unconditional, the accounting is not.
    if (ABS || playing) {
        rd = rd + BW;
        if (rd >= (ap_uint<32>)D) {
            rd = 0;
            if (playing && !loop) {
                // A pass has just finished.  THE ONE PLACE THE TWO PREDECESSORS DIFFER.
                nrep_left = nrep_left - 1;
                if (nrep_left == 0) {
                    playing = 0;
                    done_out.write((ap_uint<W>)1);
                }
            }
        }
    }

    // EXACTLY ONE POLL, outside everything above -- that is what `check_period` means, and it is
    // what keeps the datapath's II untouched.
    MemLockCmd c;
    if (memlock::mem_lock_poll(cmd_in, c)) {
        if (c.opcode == LOCK_ACQUIRE) {
            playing = 0;                    // STOP TOUCHING IT ...
            // ... AND DISARM.  Every RELEASE in practice carries a fresh play command that
            // overwrites this, but a stale arm surviving a handover would start a waveform nobody
            // asked for at the next boundary.  Clearing `playing` alone would leave it.
            pending = 0;
            memlock::mem_lock_grant<D>(resp_out, c.start_addr, c.end_addr);   // ... THEN grant
        } else {
            // A RELEASE.  The play command is already on its channel -- the loader wrote it first --
            // so this read is a bounded wait rather than a guess.  See the header note on why a
            // blocking read is safe HERE and was not in S1's requester.
            ShotPlayCmd pc;
            pc.read_stream<W>(rep_in);
            loop = (pc.opcode == (ap_uint<8>)SHOT_OP_LOOP) ? (ap_uint<1>)1 : (ap_uint<1>)0;
            nrep_left = pc.nrepeat;
            // ARMED is not PLAYING, and separating them is the second of the three edits.
            const ap_uint<1> arm = (pc.nrepeat != 0) ? (ap_uint<1>)1 : (ap_uint<1>)0;
            if (ABS) {
                // DO NOT TOUCH `rd`.  It is this task's word count since reset and a shot has no
                // business resetting it; what a new waveform gets instead is the next boundary,
                // which the test above the write loop turns into `playing`.  Bounded by one pass.
                pending = arm;
                playing = 0;
            } else {
                // A new waveform starts at its beginning: resuming mid-shot would splice the tail of
                // the old waveform's phase onto the new one, which is right in no application and is
                // invisible from a word count.
                rd = 0;
                playing = arm;
            }
            if (!arm && !loop) {
                // A finite shot that must not play -- a SHORT one.  The loader is waiting on a
                // `done` and nothing else will ever send it.
                //
                // ON `arm`, NEVER ON `pending`, AND NEVER DEFERRED.  Routing this through the
                // boundary would leave the loader blocked on a token owed for a shot that by
                // definition never plays: a DEADLOCK rather than a failing gate.
                done_out.write((ap_uint<W>)1);
            }
        }
    }
}

#endif  // WAVEFLOW_SHOT_TX_PLAYER_TASK_H
