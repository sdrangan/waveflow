#ifndef WAVEFLOW_LOCK_TOY_WRITE_TASK_H
#define WAVEFLOW_LOCK_TOY_WRITE_TASK_H
// lock_toy_write_task.h — the REQUESTER, as small as a requester gets.
//
// The C++ half of the minimal consumer for plans/t2p_lock_chan.md S1, checkpoint 2.  It exists to
// answer one question a Python-side check() cannot: does a task holding a `mode=bram` port and two
// lock channels actually SYNTHESIZE, at the II the design claims.  Everything a real requester has
// beyond this -- a header, a verdict, a repeat count -- is application, and none of it is the lock's.
//
// THE SHAPE IS THE PLAN'S, NOT A SIMPLIFICATION OF IT.
//
//     trigger  ->  ACQUIRE  ->  (payload in, memory out, ONE pipeline)  ->  RELEASE
//
// The trigger comes first because a requester ARRIVES WITH A TRANSACTION: asking for a region before
// there is anything to put in it holds the memory hostage for no reason, and the owner is the side
// that pays.  Reading it blocking is also what keeps this body on the safe side of the reset trap
// (reference-hls-task-reset-trap): a task that WRITES before it READS advances during reset, and
// this one's first act is a read.  The owner next door cannot make that choice and says so.
//
// THERE IS NO LOCAL COPY OF THE PAYLOAD, AND THAT IS THE MEASUREMENT.
//
// The obvious body reads NW words into a local array, acquires, then stores them.  That is a whole
// second buffer in LUTRAM or BRAM, and it costs NW cycles it does not need to: at II=1 a word
// arrives and a word is stored in the SAME cycle, so the two phases cost max(a, b) rather than
// a + b.  The pysim twin says exactly this by passing `get_pipelined`'s anchor into
// `write_pipelined`'s `t_start` -- and a body that buffered would make the twins two different
// designs, one of which is twice as expensive.
//
// THE COST OF HOLDING THE LOCK ACROSS A BLOCKING READ, STATED RATHER THAN HIDDEN.
//
// `s_in.read()` inside `store_shot` means a stalled input holds the region.  That is correct for
// this protocol -- the requester's obligation is to release, not to release promptly -- and it is
// the one thing a user of this interface should think about that the interface cannot decide for
// them: the owner is off part of its own memory for as long as the requester's source takes.
#include "hls_stream.h"
#include <ap_int.h>
#include "mem_lock.h"

/// @tparam W     word width in bits -- the payload's and the memory's.  NOT the lock channel's; see
///               MEM_LOCK_W in mem_lock.h for why those are separate numbers.
/// @tparam D     memory depth in elements.  The `mode=bram` array's size, which is what makes the
///               pragma take effect at all (an unsized pointer degrades to an ap_vld scalar port).
/// @tparam NW    words in one transaction -- build-time structure, so the region is [BASE, BASE+NW).
/// @tparam BASE  where the region starts.  NON-ZERO IS THE INTERESTING CASE: `base + offset` is the
///               shape of the byte-versus-word bug bram_toy stayed green through, because consistent
///               scaling round-trips perfectly right up to the top of the address space.
template <int W, int D, int NW, int BASE>
static void lock_toy_write_task(hls::stream<ap_uint<W> >& s_in,
                                ap_uint<W> buf[D],
                                memlock::chan& cmd_out,
                                memlock::chan& resp_in) {
    (void)s_in.read();                          // the trigger.  BLOCKING, and FIRST.

    memlock::mem_lock_request(cmd_out, LOCK_ACQUIRE, (ap_uint<28>)BASE, (ap_uint<28>)(BASE + NW));

    ap_uint<28> lo = 0, hi = 0;
    // BLOCKING, and bounded by the owner's check_period -- that bound is the whole reason the owner
    // declares one.  Without it this read is indistinguishable from a deadlock.
    ap_uint<8> status = memlock::mem_lock_await(resp_in, lo, hi);

    if (status == LOCK_GRANTED) {
        // LABELLED, and that is not decoration: Vitis names an unlabelled loop VITIS_LOOP_<line>_1
        // and nests that name into its children, so a comment edit above renames the synthesized
        // module -- and a gate looking the II up by name then MISSES and skips, which reads as a
        // pass.
    store_shot:
        for (int i = 0; i < NW; i++) {
#pragma HLS PIPELINE II=1
            // `lo` is the GRANT's base, not BASE: what the owner yielded is what may be touched.
            buf[lo + i] = s_in.read();
        }
        // A BARRIER, not a hint: the owner may resume the instant it sees this, so nothing below
        // may touch the region.
        memlock::mem_lock_request(cmd_out, LOCK_RELEASE, lo, hi);
    }
}

#endif  // WAVEFLOW_LOCK_TOY_WRITE_TASK_H
