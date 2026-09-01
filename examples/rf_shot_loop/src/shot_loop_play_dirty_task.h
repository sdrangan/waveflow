#ifndef WAVEFLOW_SHOT_LOOP_PLAY_DIRTY_TASK_H
#define WAVEFLOW_SHOT_LOOP_PLAY_DIRTY_TASK_H
// shot_loop_play_dirty_task.h — THE POSITIVE CONTROL.  DO NOT COPY THIS INTO A DESIGN.
//
// `waveflow/build/shot_loop_play_task.h` with ONE LINE REMOVED: the `playing = 0` that must precede
// the grant.  Everything else is byte-for-byte the shipped body.
//
// WHY A DELIBERATELY BROKEN BODY IS PART OF THE GATE.
//
// The RTL check for the collision this lock exists to prevent is a VCD scan
// (waveflow.utils.bram_trace.find_read_during_write), because bram_t2p.v's $error cannot be heard:
// XSI DISCARDS $display and $error (reference-xsi-discards-rtl-text).  And a scan that finds nothing
// is INDISTINGUISHABLE from a scan bound to the wrong nets.  So the clean run's "no hazards" means
// nothing on its own; what makes it evidence is that the SAME scan, on the SAME manifest, finds
// hazards in a run known to collide.  That run is this file.
//
// It has to be THIS design with one line changed rather than a separately written broken one: a
// different design would exercise different nets and prove nothing about the shipped one.  The seam
// that makes it possible without copying the composite is RfShotTxLoop.player_cls.
//
// WHAT IT DOES WRONG, PRECISELY.
//
// It answers the ACQUIRE while still in the playing state, so the loader begins writing the region
// on the next cycle while this task is still reading it -- both ports of the memory live on the same
// addresses.  In pysim that is a RuntimeError from LockedMemSlaveIF (the region is out of the
// owner's hands the moment grant() returns); at RTL it is silent, and the waveform is the only
// witness.
#include "hls_stream.h"
#include <ap_int.h>
#include "mem_lock.h"

#ifndef SHOT_LOOP_FILLER
#define SHOT_LOOP_FILLER 0
#endif

/// The shipped body's signature exactly -- see shot_loop_play_task.h for what each parameter means.
template <int W, int D, int NW, int BASE, int BW>
static void shot_loop_play_dirty_task(ap_uint<W> buf[D],
                                      memlock::chan& cmd_in,
                                      memlock::chan& resp_out,
                                      hls::stream<ap_uint<W> >& samp_out) {
    // The shipped body starts at 0 for the reason given there.  This one starts at 1 so the memory
    // is ALREADY being read when the first ACQUIRE arrives -- the control has to collide on its
    // first handover, not eventually.
    static ap_uint<1> playing = 1;
#pragma HLS reset variable=playing
    static ap_uint<32> rd = 0;
#pragma HLS reset variable=rd

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
            // THE DEFECT, AND IT IS THE WHOLE FILE: no `playing = 0` here.  The region goes out and
            // this task carries on reading it.
            //
            // WHAT THIS CONTROL SHOWS, AND WHAT IT DOES NOT.  MEASURED 2026-09-01: it puts both
            // ports live on the same 64-word region for 34 cycles of the run -- and
            // find_read_during_write, which implements bram_t2p.v's CYCLE-EXACT predicate, still
            // reports NOTHING.  The reader trails the writer by one word and then falls behind at
            // 5 words per sample (it is DAC-paced at one word in four; the writer runs at II=1), so
            // the two sweeps cross inside the reader's idle window every time.  That is
            // examples/bram_access's finding restated: ADDRESS OVERLAP ALONE IS NOT A COLLISION --
            // two sweeps over one range are parallel lines in (cycle, address) and meet only if
            // they are in phase.
            //
            // Aligning them on purpose was tried (resetting `rd` here so both start at the region's
            // base together) and did not help: the phase is deterministic and lands the crossing in
            // the same idle window.  So the gate pairs on the condition the LOCK actually removes --
            // both ports live on one region at once -- and asserts the cycle-exact scan separately
            // on the shipped design.  See tests/examples/test_rf_shot_loop_xsi.py.
            memlock::mem_lock_grant<D>(resp_out, c.start_addr, c.end_addr);
        } else {
            rd = 0;
            playing = 1;
        }
    }
}

#endif  // WAVEFLOW_SHOT_LOOP_PLAY_DIRTY_TASK_H
