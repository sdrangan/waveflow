#ifndef WAVEFLOW_LOCK_TOY_READ_TASK_H
#define WAVEFLOW_LOCK_TOY_READ_TASK_H
// lock_toy_read_task.h — the OWNER, and the ordering everything turns on.
//
// The C++ half of the minimal consumer for plans/t2p_lock_chan.md S1, checkpoint 2.  It holds the
// whole memory, reads a chunk per firing, and polls the lock channel exactly once between chunks.
//
// THE POLL SITS OUTSIDE THE PIPELINED LOOP, AND THAT IS WHY II=1 SURVIVES.
//
// One poll per CHECK_PERIOD elements: the datapath is a counted loop with nothing data-dependent in
// its trip count, and the lock traffic is the outer loop's.  This is also the definition of
// check_period -- the maximum elements of its own work an owner may do between polls -- so the
// requester's blocking wait for a grant is a STATED NUMBER, and a gate can assert it.
//
// SET THE STATE BEFORE YOU GRANT.  ALWAYS.
//
// `playing = 0` comes BEFORE mem_lock_grant().  Granting while still reading lets the requester
// write memory this task is reading, which is precisely the collision the interface exists to
// prevent -- bram_t2p.v's $error catches it at RTL, and XSI throws $error away
// (reference-xsi-discards-rtl-text), so nothing here would say a word.  pysim's LockedMemSlaveIF
// takes the region out of the owner's hands inside grant() and raises on the very next access, which
// is why the pysim gate is what proves this ordering and the waveform is not.
//
// THIS BODY WRITES BEFORE IT READS, AND THEREFORE THE RESET TRAP APPLIES.
//
// reference-hls-task-reset-trap: an hls::task that WRITES before it READS advances during reset, so
// its statics move before the design is alive.  This owner cannot avoid it -- writing without being
// asked is what "the side that cannot stop" MEANS -- so it carries `#pragma HLS reset` on both
// statics AND needs `config_rtl -reset state` in the solution tcl, which is what actually closed it
// under Vitis 2025.1 when the pragma alone did not (measured in rf_repeat_play).  Any owner written
// against this interface inherits that obligation; the requester next door does not.
//
// `rd` IS A RUNNING BASE, DELIBERATELY.
//
// `buf[rd + i]` with `rd` a static is the dynamic base addressing plans/t2p_lock_chan.md names as
// the silent-failure class this repo has already paid for once.  A body indexing `buf[i]` would
// synthesize just as well and would measure nothing.
#include "hls_stream.h"
#include <ap_int.h>
#include "mem_lock.h"

/// @tparam W   word width in bits -- the memory's and the output stream's.
/// @tparam D   memory depth in elements.  Also the wrap point of `rd`, and the bound
///             mem_lock_grant refuses a region against.
/// @tparam CP  check_period: elements of its own work between polls.  Must divide D, so a chunk
///             never straddles the wrap -- a chunk that did would need two base additions and the
///             body would be measuring something else.
template <int W, int D, int CP>
static void lock_toy_read_task(ap_uint<W> buf[D],
                               memlock::chan& cmd_in,
                               memlock::chan& resp_out,
                               hls::stream<ap_uint<W> >& s_out) {
    //: 1 while this task owns the region it is reading; 0 while it has yielded it.  ONE BIT is
    //: enough at S1 because the owner yields the WHOLE memory -- the region is enforcement in pysim
    //: and documentation here, which plans/t2p_lock_chan.md says out loud rather than implying.
    static ap_uint<1> playing = 1;
#pragma HLS reset variable=playing
    //: The running read base.  See the header note on dynamic base addressing.
    static ap_uint<32> rd = 0;
#pragma HLS reset variable=rd

    // LABELLED -- see the note in lock_toy_write_task.h.  The II gate looks this module up by name.
play_chunk:
    for (int i = 0; i < CP; i++) {
#pragma HLS PIPELINE II=1
        // The filler is a zero rather than a stall: the owner CANNOT STOP.  A body that blocked
        // here while yielded would back-pressure whatever it feeds, which on a converter is not an
        // option and is the whole reason this side is the owner.
        ap_uint<W> v = playing ? buf[rd + i] : (ap_uint<W>)0;
        s_out.write(v);
    }
    if (playing) {
        rd = rd + CP;
        if (rd >= (ap_uint<32>)D) {
            rd = 0;
        }
    }

    MemLockCmd c;
    if (memlock::mem_lock_poll(cmd_in, c)) {
        if (c.opcode == LOCK_ACQUIRE) {
            playing = 0;                    // STOP TOUCHING IT, then grant.  THIS ORDER.
            memlock::mem_lock_grant<D>(resp_out, c.start_addr, c.end_addr);
        } else {
            playing = 1;                    // RELEASE is not answered; there is nothing to race.
        }
    }
}

#endif  // WAVEFLOW_LOCK_TOY_READ_TASK_H
