#ifndef WAVEFLOW_MEM_LOCK_H
#define WAVEFLOW_MEM_LOCK_H
// mem_lock.h — the lock channel's C++ half: the codes, the width, and the three moves.
//
// plans/t2p_lock_chan.md S1.  The twin of waveflow/hw/locked_mem.py, and NOT generated from it: the
// Python side is the pysim golden and this is the hardware, written independently to the same
// contract so the gates can catch them diverging.  What IS shared is the bit layout — both ends
// reach it through the generated mem_lock_cmd.h / mem_lock_resp.h, so no body here touches a range.
//
// THE CODES ARE LITERALS, NOT AN ENUM.
//
// rf_tx_loader_task.h's convention, for its reason: the generated schema header and a hand-written
// body cannot disagree about an encoding neither of them defines.  Each code is a distinct REPAIR --
// LOCK_BAD_RANGE is a caller that asked for a region the memory does not have, and collapsing it
// into "not granted" would report one fault as another.
//
// MEM_LOCK_W IS 64 AND IS NOT THE DESIGN'S WORD WIDTH.
//
// 8 + 28 + 28.  The channel is built at the SCHEMA's width, never the memory's -- a channel whose
// width can disagree with what travels on it is a disagreement waiting to be found at the wrap
// (status_bitwidth's argument next door).  So a 32-bit design still gets 64-bit lock FIFOs, and the
// two backends cannot end up with a two-beat command that a non-blocking poll reads half of.
//
// THE OWNER'S POLL IS NON-BLOCKING AND THE REQUESTER'S WAIT IS NOT.
//
// That asymmetry is the protocol.  The owner cannot stop -- an empty command channel means NO NEWS,
// never "wait" -- so it polls once per check_period elements of its own work, which is what makes
// the requester's blocking wait a STATED NUMBER rather than a hope.  Without such a bound "the owner
// is busy" is indistinguishable from a deadlock.
//
// AND THE ORDERING EVERYTHING TURNS ON.
//
// An owner must stop touching the region BEFORE it calls grant().  Granting while still reading lets
// the requester write memory the owner is reading -- precisely the collision this interface exists
// to prevent, and precisely what bram_t2p.v's $error would catch at RTL if XSI did not throw $error
// away.  There is no way to enforce it from here: grant() cannot know what the caller's state
// machine is doing.  pysim's LockedMemSlaveIF CAN, and does, which is why the pysim gate is the one
// that proves this rather than the waveform.
#include "hls_stream.h"
#include <ap_int.h>
#include "mem_lock_cmd.h"
#include "mem_lock_resp.h"

//: One lock beat.  See the note above -- this is the schema's width, not the design's.
#define MEM_LOCK_W 64

//: MemLockCmd.opcode.
#define LOCK_ACQUIRE 0
#define LOCK_RELEASE 1

//: MemLockResp.status.
#define LOCK_GRANTED   0
#define LOCK_BAD_RANGE 1

namespace memlock {

//: A lock channel.  Both directions are the same type; which way it runs is which argument it is.
typedef hls::stream<ap_uint<MEM_LOCK_W> > chan;

/// Requester: put one command on the wire.  Used for both ACQUIRE and RELEASE.
///
/// RELEASE carries the region even though S1 has only one and nothing correlates it.  Same reason
/// the grant echoes: a waveform is readable without cross-referencing the command that produced it,
/// and S2 needs the correlation without a format change.
static inline void mem_lock_request(chan& cmd_out, ap_uint<8> opcode,
                                    ap_uint<28> lo, ap_uint<28> hi) {
    MemLockCmd c;
    c.opcode = opcode;
    c.start_addr = lo;
    c.end_addr = hi;
    c.write_stream<MEM_LOCK_W>(cmd_out);
}

/// Requester: BLOCK for the answer.  Returns the status and hands back the region that was granted.
///
/// The GRANT's region, not the request's.  What the owner yielded is what may be touched, and
/// believing the request instead would make a clamp invisible on this side -- S1 refuses to clamp,
/// but a body written against the request would not notice if S2 ever did.
static inline ap_uint<8> mem_lock_await(chan& resp_in, ap_uint<28>& lo, ap_uint<28>& hi) {
    MemLockResp r;
    r.read_stream<MEM_LOCK_W>(resp_in);
    lo = r.start_addr;
    hi = r.end_addr;
    return r.status;
}

/// Owner: take a command if one is waiting.  NEVER blocks; returns false for "no news".
///
/// One beat, so read_nb is enough and MemLockCmd::unpack_from_uint is the whole decode.  A schema
/// that grew past one beat would make this read half a command with nothing to say so, which is why
/// lock_bitwidth() refuses one on the Python side.
static inline bool mem_lock_poll(chan& cmd_in, MemLockCmd& c) {
    ap_uint<MEM_LOCK_W> w;
    if (!cmd_in.read_nb(w)) {
        return false;
    }
    c = MemLockCmd::unpack_from_uint(w);
    return true;
}

/// Owner: answer an ACQUIRE.  Returns the status that went out.
///
/// @tparam NELEM  the memory's depth in ELEMENTS -- build-time structure, and the single source for
///                the bound.  A region is refused, never clamped: a clamped region is a different
///                region, silently, which is SHOT_WRONG_LEN's argument applied to an address range.
///
/// CALL THIS ONLY AFTER THE CALLER HAS STOPPED TOUCHING THE REGION.  See the header note.
template <int NELEM>
static inline ap_uint<8> mem_lock_grant(chan& resp_out, ap_uint<28> lo, ap_uint<28> hi) {
    // Widened by one bit before the compare: NELEM may be 2^28 exactly, which does not fit the
    // field, and an unsigned compare that wrapped would GRANT the region it meant to refuse.
    ap_uint<8> status = ((ap_uint<29>)lo <= (ap_uint<29>)hi && (ap_uint<29>)hi <= (ap_uint<29>)NELEM)
                        ? (ap_uint<8>)LOCK_GRANTED : (ap_uint<8>)LOCK_BAD_RANGE;
    MemLockResp r;
    r.status = status;
    r.start_addr = lo;
    r.end_addr = hi;
    r.write_stream<MEM_LOCK_W>(resp_out);
    return status;
}

}  // namespace memlock

#endif  // WAVEFLOW_MEM_LOCK_H
