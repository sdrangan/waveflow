#ifndef WAVEFLOW_RF_TX_LOADER_TASK_H
#define WAVEFLOW_RF_TX_LOADER_TASK_H
// rf_tx_loader_task.h — the streaming transmitter's LOADER: one command per firing, tagged samples
// out, one deferred response per command.
//
// A BLOCKING WRITE IS CORRECT HERE, so this body is plain: no credit, no avail(), NO SPIN.
//
// That is the point of the whole design.  On TX the producer is your own logic, so a stall costs it
// time and nothing else — which is why rf_samp_buf_loader_task.h's data-dependent
// `while (!room) { ... }` around a progress channel can go.  That spin is what makes its body
// unschedulable as an outer loop (HLS 200-878, HLS 200-960) and holds it at 2 cycles/word against a
// converter-facing body that reaches 1.
//
// EVERY LOOP HERE IS COUNTED.  The payload loop trips c.nsamp times; the harvest trips POLLS times
// with POLLS a compile-time constant.  Nothing is data-dependent in its TRIP COUNT — a `break` on a
// data-dependent CONDITION is fine and was measured at II=1 (plans/witness/task_loop,
// scratchpad/trig).  It is the unbounded inner loop that costs the II, not the exit test.
//
// RESPONSES ARE DEFERRED, AND THAT IS THE MECHANISM.
//
// A refusal answers IMMEDIATELY — nothing was loaded, so there is nothing to wait for.  An accepted
// command is NOT answered at acceptance: it is pushed onto the pending FIFO as {tid, slot, nsamp,
// now} and answered when the player's verdict for that window comes back.  So there is exactly one
// TxResp per command either way, and the accepted one carries the truth rather than a promise.
//
// ORDERING NEEDS NO MATCHING.  The player processes samples in slot order and windows are loaded in
// order, so statuses return in the order commands were pushed.  pending.pop() in order is correct;
// matching by tid would be redundant machinery.  EVERY STATUS IS A REPLY — there is no heartbeat and
// no unsolicited traffic — so the pop is unconditional and there is no "is this for me?" test.
//
// THERE IS NO TOO-LATE PRE-CHECK, deliberately.  The player already detects lateness
// (BEFORE -> MISSED) and a second detector here would be fed by a staler view of the same fact.  Two
// sources of truth for one condition is the failure mode this design exists to remove.  A doomed
// window costs stream bandwidth and nothing at the DAC.
//
// pending FULL IS AN ADMISSION CONDITION, NOT AN ASSERTION.  A command accepted with nowhere to
// record its tid would break the correspondence SILENTLY, so a full FIFO is a refusal
// (RF_TX_NO_SLOT), counted like any other.  One constant governs both channels: MAX_IN_FLIGHT is the
// pending depth AND the bound the status FIFO is sized against.
//
// nsamp == 0 IS REFUSED, beside misaligned.  A zero-length window has no last sample, so
// request_status is never set, so no status returns and the pending slot never pops — a few of those
// and this task refuses everything with RF_TX_NO_SLOT for reasons that look nothing like the cause.
// It gets a code of its own rather than sharing MISALIGNED: a length fault and a geometry fault are
// different repairs.
//
// A REFUSED COMMAND MUST STILL DRAIN ITS PAYLOAD.  Otherwise the next command's samples are read as
// this one's and every command after it is misaligned.  That is why the refuse path has a counted
// loop rather than an early exit — and why, in both twins, the drain sits OUTSIDE the branch.
#include "hls_stream.h"
#include <ap_int.h>
#include "rf_tx_cmd.h"
#include "rf_tx_resp.h"
#include "rf_tx_status.h"
#include "rf_tagged_samp.h"

//: TxResp.status codes.  Literals rather than an enum, so the generated schema header and this body
//: cannot disagree about the encoding.  Each code is a distinct REPAIR; collapsing any two would
//: report one fault as another.
#define RF_TX_TRANSMITTED 0
#define RF_TX_TOO_LATE    1
#define RF_TX_MISALIGNED  2
#define RF_TX_NO_SLOT     3
#define RF_TX_ZERO_LEN    4

//: TxStatus.verdict, mirrored from rf_tx_player_task.h — one encoding, stated in both bodies rather
//: than in a shared header neither would then own.
#ifndef RF_TX_PLAYED
#define RF_TX_PLAYED 0
#define RF_TX_MISSED 1
#endif

/// @tparam W       AXIS word width in bits for the command / sample / response ports.
/// @tparam SPW     samples per word (power of two).  Only SPW == 1 is exercised by the gate.
/// @tparam MAXIF   MAX_IN_FLIGHT: the pending FIFO depth, and the bound the status FIFO is sized
///                 against.  MUST be a power of two — the ring index is a mask.
/// @tparam POLLS   statuses harvested per firing.  BOUNDED and compile-time: this unrolls into POLLS
///                 read_nb calls.  `while (got)` is the data-dependent trip count that costs the old
///                 design its II.
/// @tparam TAG_W   width of the two INTERNAL channels, in bits.  They carry `TaggedSamp` and
///                 `TxStatus` **packed into one word each** by the generated `pack_to_uint` /
///                 `unpack_from_uint`, rather than as struct-typed FIFOs.  Two reasons, and the
///                 second is the one that matters: a word channel is what
///                 `composite_gen`'s `StreamEdge` already lowers, so no new edge kind is needed;
///                 and it is **exactly what the pysim twin puts on the wire**, so the two backends
///                 carry identical bits rather than two representations that must be kept in step.
/// @tparam IDX_W   width of the slot counter and of every TxCmd/TxResp/TxStatus index field.
template <int W, int TAG_W, int SPW, int MAXIF, int POLLS, int IDX_W>
/// ARGUMENT ORDER IS PART OF THE CONTRACT.  `to_player` is ONE endpoint on the Python module
/// (an AckedStreamMasterIF) and TWO channels here, and the composite generator splices them in
/// **adjacent, in physical_endpoints() order** at the position that endpoint's name occupies in
/// `kernel_task().signature`.  So the forward and status streams sit together rather than at
/// positions 3 and 5 as an earlier draft had them: the alternative is a second naming scheme
/// (`"to_player.ack"`) that only the resolver would understand.
static void rf_tx_loader_task(hls::stream<ap_uint<W> >& cmd_in, hls::stream<ap_uint<W> >& samp_in,
                              hls::stream<ap_uint<TAG_W> >& to_player,
                              hls::stream<ap_uint<TAG_W> >& status_in,
                              hls::stream<ap_uint<W> >& resp_out) {
    // The pending FIFO — a ring, held as parallel arrays rather than an array of structs so a
    // nested struct cannot be copied by value into the task's interface (which DCEs the kernel; see
    // reference-hls-hook-csynth-gotchas).
    // The payload arrays need no reset: nothing reads a slot that was not written first, and
    // resetting MAXIF registers per array would cost real logic to restore values that are already
    // dead.  The INDICES do — see the reset note below.
    static ap_uint<IDX_W> pd_tid[MAXIF];
    static ap_uint<IDX_W> pd_slot[MAXIF];
    static ap_uint<IDX_W> pd_nsamp[MAXIF];
    static ap_uint<1> pd_now[MAXIF];
    // THE RESET TRAP.  A `static` with `= 0` becomes a simulation initial value in the RTL, NOT a
    // reset, and its update is emitted with no ap_rst term.  Every task that begins with a BLOCKING
    // read survives that — at reset its input is empty, it stalls, and its state does not move.
    // This one begins with a non-blocking harvest and a non-blocking poll, so nothing stalls it, and
    // the XSI harness holds reset 16 cycles.  pysim cannot see this at all: SimPy has no reset.
    //
    // The pragmas below did NOT take under Vitis 2025.1 — see the note in rf_tx_player_task.h.  What
    // closes it is `config_rtl -reset state` at the solution level.
    static ap_uint<IDX_W> pd_head = 0;      // next to pop  — the OLDEST unresolved window
#pragma HLS reset variable=pd_head
    static ap_uint<IDX_W> pd_tail = 0;      // next to push
#pragma HLS reset variable=pd_tail
    static ap_uint<IDX_W> pd_count = 0;
#pragma HLS reset variable=pd_count

    // -- 1. HARVEST.  Answer whatever the player resolved since the last firing. ----------------
    //
    // BOUNDED: POLLS is compile-time, so this unrolls.  Every status is a reply, so the pop is
    // unconditional — there is no id to match and nothing to test.
    for (int i = 0; i < POLLS; i = i + 1) {
#pragma HLS PIPELINE II=1
        ap_uint<TAG_W> stw;
        if (!status_in.read_nb(stw)) {
            break;                          // a data-dependent EXIT on a COUNTED loop: II=1, measured
        }
        TxStatus st = TxStatus::unpack_from_uint(stw.range(TxStatus::bitwidth - 1, 0));
        if (pd_count == 0) {
            continue;                       // unreachable while the contract holds; never silent
        }
        ap_uint<IDX_W> tid = pd_tid[pd_head & (MAXIF - 1)];
        ap_uint<IDX_W> slot = pd_slot[pd_head & (MAXIF - 1)];
        ap_uint<IDX_W> nsamp = pd_nsamp[pd_head & (MAXIF - 1)];
        ap_uint<1> was_now = pd_now[pd_head & (MAXIF - 1)];
        pd_head = pd_head + 1;
        pd_count = pd_count - 1;

        bool played = (st.verdict == RF_TX_PLAYED);
        // A start_now window had no slot until the player assigned one.  Recover it from the status,
        // which reports where the LAST sample of the window actually went out — this is the whole
        // mechanism by which a host learns "now", and the only place TxStatus.slot is used.
        ap_uint<IDX_W> start = (was_now && played) ? (ap_uint<IDX_W>)(st.slot - (nsamp - 1)) : slot;

        TxResp r;
        r.tid = tid;
        r.status = played ? RF_TX_TRANSMITTED : RF_TX_TOO_LATE;
        r.samp_start = start;
        r.write_stream<W>(resp_out);
    }

    // -- 2. NO_CMD.  Poll for a command; if there is none, this firing is done. ------------------
    //
    // NON-BLOCKING, and it is not a style choice: with responses deferred, a host that waits for its
    // TxResp before sending the next command and a loader that waits for the next command before
    // harvesting are waiting for each other.  A blocking read here deadlocks on the FIRST window.
    // `empty()` then a blocking read, rather than a read_nb per word: a TxCmd is several words and
    // hls::stream has no multi-word peek.  The words of one command arrive as one burst, so the
    // blocking read that follows is never a wait for something that is not coming — and the test
    // that matters (is there a command AT ALL?) is answered without waiting.
    if (cmd_in.empty()) {
        return;
    }
    TxCmd c;
    c.read_stream<W>(cmd_in);

    // -- 3. ADMIT. ------------------------------------------------------------------------------
    ap_uint<IDX_W> slot = c.samp_start;     // ignored when start_now: the player assigns it
    ap_uint<IDX_W> status = RF_TX_TRANSMITTED;
    bool refused = false;

    if (c.nsamp == 0) {
        status = RF_TX_ZERO_LEN;
        refused = true;
    } else if ((c.samp_start % SPW) != 0 || (c.nsamp % SPW) != 0) {
        // Both halves matter: a window that STARTS mid-word is as unservable as one that ENDS
        // mid-word, and only one of the two is obvious.  At SPW == 1 the whole branch folds away.
        status = RF_TX_MISALIGNED;
        refused = true;
    } else if (pd_count >= MAXIF) {
        status = RF_TX_NO_SLOT;
        refused = true;
    }

    if (refused) {
        TxResp r;                           // refusals answer IMMEDIATELY; nothing pends
        r.tid = c.tid;
        r.status = status;
        r.samp_start = slot;
        r.write_stream<W>(resp_out);
    } else {
        pd_tid[pd_tail & (MAXIF - 1)] = c.tid;
        pd_slot[pd_tail & (MAXIF - 1)] = slot;
        pd_nsamp[pd_tail & (MAXIF - 1)] = c.nsamp;
        pd_now[pd_tail & (MAXIF - 1)] = (ap_uint<1>)(c.start_now != 0);
        pd_tail = pd_tail + 1;
        pd_count = pd_count + 1;
    }

    // A zero-length command carries no payload frame, so there is nothing to drain and reading would
    // consume the NEXT command's samples — the very desynchronisation the drain exists to prevent,
    // arrived at from the other side.
    if (c.nsamp == 0) {
        return;
    }

    // -- 4. LOAD (or DRAIN).  Counted, and it runs whatever the verdict. -------------------------
    ap_uint<IDX_W> npay = (c.nsamp + SPW - 1) / SPW;
    for (ap_uint<IDX_W> i = 0; i < npay; i = i + 1) {
#pragma HLS PIPELINE II=1
        ap_uint<W> x = samp_in.read();      // ALWAYS consume, so the frame stays aligned
        if (refused) {
            continue;                       // consumed and discarded, never silently sent
        }
        for (int j = 0; j < SPW; j = j + 1) {
#pragma HLS UNROLL
            ap_uint<IDX_W> k = (ap_uint<IDX_W>)(i * SPW + j);
            TaggedSamp t;
            t.wr = (ap_uint<IDX_W>)(slot + k);          // ignored when `now`
            t.now = (ap_uint<1>)(c.start_now != 0);
            // THE LAST SAMPLE ANSWERS THE QUESTION.  One request per window is what bounds the
            // reverse rate by construction — the structural half of the saturation rule.
            t.request_status = (ap_uint<1>)(k == (ap_uint<IDX_W>)(c.nsamp - 1));
            t.samp = x.range((j + 1) * (W / SPW) - 1, j * (W / SPW));
            ap_uint<TAG_W> tw = 0;
            tw.range(TaggedSamp::bitwidth - 1, 0) = TaggedSamp::pack_to_uint(t);
            to_player.write(tw);            // BLOCKING is correct: stalling is our problem
        }
    }
}

#endif  // WAVEFLOW_RF_TX_LOADER_TASK_H
