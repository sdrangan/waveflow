#ifndef WAVEFLOW_RF_TX_PLAYER_TASK_H
#define WAVEFLOW_RF_TX_PLAYER_TASK_H
// rf_tx_player_task.h — the streaming transmitter's PLAYER: one slot per firing, forever.
//
// THE NEVER-MISS-A-DEADLINE LAW APPLIES TO THIS TASK, AND ONLY TO THIS TASK.
//
// A DAC consumes a word every sample period whether or not one is ready, so this body may never wait
// for the loader.  It fails by UNDERRUNNING — a slot came due and nothing had been loaded for it —
// which is the mirror of the RX ingress's overrun, and the reason nothing here blocks except the one
// write that IS the metronome.
//
// WHAT IS NEW HERE, against rf_samp_buf_player_task.h: there is no buffer and no play pointer to
// compare against a progress channel.  Each sample carries the slot it is for, so the decision is
// local — a three-way compare between the held sample's tag and this task's own slot counter — and
// the loader learns the outcome from an ACK rather than from a position.  That is the whole
// difference between the two designs, in one branch.
//
// THREE-WAY, AND IT BREAKS ON >= RATHER THAN ==.
//
//   BEFORE  the sample's slot has already gone out       -> discard it, report MISSED
//   AT      this is its slot                             -> emit it, report PLAYED
//   AFTER   its slot has not arrived yet                 -> HOLD it, emit a filler, count an underrun
//
// A missed sample then costs a one-sample-late start instead of a full counter wrap: at 32 bits and
// 1 GSa/s a wrap is 4.3 s, delivered with a valid-looking response, which is worse than a hang
// because a hang is obvious.  There is no "reasonable lateness" tolerance and there must not be —
// any such threshold is MARGIN in a new costume, and it silently changes what a command meant.
//
// THE HALF-WRAP CONTRACT.  time_compare is meaningful only while the two values are within
// 2^(IDX_W-1) of each other.  At 16 bits and 64 MSa/s that is 512 us; at 32 bits and 1 GSa/s, 2.1 s.
// It is a real bound and it belongs written beside IDX_W, because the whole scheme rests on it.
//
// THE STATUS IS SOLICITED, NEVER BROADCAST.  It is emitted only when a sample carrying
// request_status leaves the holding register — normally the last of each window, which is precisely
// the "did my window go out on time?" question.  A per-slot write would saturate the FIFO, after
// which write_nb drops the NEWEST while the reader pops the OLDEST: the loader would read ancient
// numbers forever and "the newest supersedes" would quietly stop being true.  That is a correctness
// property, not a tuning one.
//
// n_status_dropped IS EXPECTED PERMANENTLY ZERO, because one request per accepted window means the
// FIFO can never need more than MAX_IN_FLIGHT entries — the same constant that bounds the loader's
// pending FIFO.  Non-zero means the sizing rule was violated, and the symptom otherwise is a
// verdict paired with the wrong tid, which looks exactly like a verdict.
//
// WHY BEFORE DOES NOT COUNT AN UNDERRUN.  The slot that sample missed was already filled by a filler
// and already counted, at the moment it came due.  Counting it again would double-report one event.
// The old design's separate too_late counter is redundant under a cumulative status.
//
// THE SHAPE IS `while (1)` + `#pragma HLS PIPELINE II=1`, AND THAT IS THE WHOLE THROUGHPUT STORY.
//
// The first build of this body had no loop at all -- one slot per firing, letting the hls::task
// runtime re-fire it, which is what rf_samp_buf_player_task.h does.  That is measurably the wrong
// shape.  plans/witness/task_loop/ measured both, in RTL:
//
//   ply_1  (per-firing)   3.000 cycles/word   399 boundary gaps of 3 cycles, one per firing
//   ply_w  (`while (1)`)  1.000 cycles/word   no gaps at all
//
// The per-firing body pays a fixed re-entry cost on EVERY word; the loop pays it once.  At
// samp_per_word = 4 and 250 MHz that is the difference between a 333 MSPS design ceiling and the
// port's own 1000 MSPS.  A body that stops reading for 3 cycles per word also drops converter words
// at high rates -- see the witness's boundary-loss arithmetic -- while a loop never stops.
//
// A `while (1)` BODY REPORTS NO LATENCY AND `TripCount = inf`.  That is correct, not a failure: an
// unbounded body has no cycles-per-firing to report, and `fire_cycles = latency + 1` is meaningless
// for it.  Its throughput is measured AT RTL.  Reading csynth's silence as a bad number is how the
// first pass talked itself into refuting a target it had never measured.
//
// IT REACHES II=1 AND IT DOES NOT CLOSE AT 4.0 ns.  Measured: `PipelineII = 1`, depth 4, estimated
// Fmax 206.95 MHz against the RF arc's 250.  This is a REAL result and it is new -- the witness's
// `ply_w` body was a BRAM read and a stream write with no decision in it, so it had no such path.
// The recurrence, quoted from `HLS 200-1016`:
//
//     slot --> sub (h.wr - slot) --> icmp --> and --> phi --> phi --> phi --> fwd_read ENABLE
//     0.853 + 0.853 + 0.287 + 0.476 + 0.476 + 0.427 + 1.460 = 4.8 ns
//
// i.e. **whether to read a new sample this cycle depends on whether the held one was consumed,
// which depends on the compare against `slot`**.  That is a genuine loop-carried dependency, not a
// coding accident, and shortening the compare only bought 3 MHz (204.00 -> 206.95: writing the
// three-way test as a raw signed difference removes one `select` level, which is kept because it is
// also clearer).
//
// THE FIX, IDENTIFIED AND NOT BUILT: a two-deep skid.  Read into `h_next` whenever `h_next` is free
// -- a condition that does NOT involve the compare -- and let the compare only move `h_next` into
// `h`.  The read enable then depends on last cycle's shift, one level of logic, and the compare
// falls out of the recurrence.  That is micro-architecture work and it belongs to Stage 3, where the
// ceiling is the deliverable; Stage 1's job was to build the specified shape and measure it.
//
// What it means for the ceiling meanwhile: 1 cycle/word at 207 MHz is 207 MSa/s per sample-per-word,
// against the per-firing shape's 3 cycles/word at 250 MHz = 83.3.  **2.5x, measured**, and the gap
// to the port's 250 is a timing problem with a known fix rather than a throughput one.
//
// THE RESET TRAP APPLIES TO THIS BODY.  It WRITES (a filler) before it has read anything blocking,
// and an empty output FIFO always has room — so without a reset its state advances once per reset
// cycle, and the XSI harness holds reset 16 cycles.  A slot counter that comes out of reset at 16
// puts every window 16 slots late, for reasons that look nothing like the cause.  pysim cannot see
// this at all: SimPy has no reset.
//
// AND `while (1)` DOES NOT MAKE THAT WORSE -- measured, because plans/witness/task_loop/ left
// "`while (1)` under reset" as an open question and this is the first body in a position to answer
// it.  See examples/rf_repeat_play/rf_repeat_play_build.py for the numbers.  The shape of the
// question does change: a per-firing body re-enters its FSM every firing, so its state advances once
// per firing during reset, whereas a looped body enters once and its state lives in pipeline
// registers.  What closes it is the same thing either way.
//
// MEASURED 2026-08-20, Vitis HLS 2025.1: THE `#pragma HLS reset` LINES BELOW DID NOT TAKE.  csynth
// reported "WARNING: [RTGEN 206-101] Register '<name>' is power-on initialization" for every one of
// them.  What closes the trap is `config_rtl -reset state` at the SOLUTION level.  They are kept
// here because they state the intent at the point it applies, and because a future Vitis may honour
// them; a build that relies on them ALONE is relying on nothing.
#include "hls_stream.h"
#include <ap_int.h>
#include "rf_tagged_samp.h"
#include "rf_tx_status.h"

//: TxStatus.verdict.  Literals rather than an enum, so the generated schema header and this body
//: cannot disagree about the encoding.
#define RF_TX_PLAYED 0
#define RF_TX_MISSED 1

//: The three-way compare, as a signed circular difference.  `< 0` is BEFORE, `0` is AT, `> 0` is
//: AFTER.  A plain `a < b` on a wrapping counter is wrong the first time it wraps, and wrong
//: SILENTLY; this is exact while the two are within a half wrap of each other.
template <int IDX_W>
static ap_int<IDX_W> rf_tx_time_compare(ap_uint<IDX_W> a, ap_uint<IDX_W> b) {
#pragma HLS INLINE
    return (ap_int<IDX_W>)(a - b);
}

/// @tparam W      AXIS word width in bits — SPW samples of W/SPW bits each.
/// @tparam SPW    samples per word.  MUST be a power of two: the sample->word conversion is then a
///                shift, and a divide in the never-miss path costs cycles the DAC does not give
///                back.  Only SPW == 1 is exercised by the gate.
/// @tparam IDX_W  width of the SLOT counter and of every TxCmd/TxResp/TxStatus index field.
///                Deliberately independent of W: widening the word must not move where a slot
///                counter wraps.
/// @tparam TAG_W  width of the two INTERNAL channels -- see rf_tx_loader_task.h.  `fwd` is ONE
///                endpoint on the Python module and TWO channels here, spliced in adjacent.
template <int W, int TAG_W, int SPW, int IDX_W>
static void rf_tx_player_task(hls::stream<ap_uint<TAG_W> >& fwd,
                              hls::stream<ap_uint<TAG_W> >& status_out,
                              hls::stream<ap_uint<W> >& samp_out) {
    // The slot counter.  Free-running from 0, exactly as the hardware is: the DAC's grid starts when
    // the tile does, whether or not anything has been loaded.
    static ap_uint<IDX_W> slot = 0;
#pragma HLS reset variable=slot
    // Highest slot emitted from REAL data.  It does NOT advance while idle, which is why a heartbeat
    // built on it would have refreshed nothing — the reason there is no heartbeat in this design.
    static ap_uint<IDX_W> played_through = 0;
#pragma HLS reset variable=played_through
    // Slots filled because nothing was ready.  Cumulative, so a lost status is harmless.
    static ap_uint<IDX_W> n_underrun = 0;
#pragma HLS reset variable=n_underrun
    // Statuses the FIFO refused.  Expected permanently zero; see the header.
    static ap_uint<IDX_W> n_status_dropped = 0;
#pragma HLS reset variable=n_status_dropped
    // The holding register — hls::stream has no peek, so a sample that is not due yet is held here
    // rather than left in the FIFO.  ONE element is enough: samples arrive in slot order.
    static TaggedSamp h;
#pragma HLS reset variable=h
    static bool held = false;
#pragma HLS reset variable=held
    // The filler.  ZERO, deliberately, and the choice is written here because the real RFDC repeats
    // its last frame instead (plans/adc_model.md).  The counter means the same thing either way; a
    // scope trace will not match a simulation, and that is a declared difference rather than a bug.
    const ap_uint<W> filler = 0;

    while (1) {
#pragma HLS PIPELINE II=1
        ap_uint<TAG_W> hw;
        if (!held && fwd.read_nb(hw)) {
            h = TaggedSamp::unpack_from_uint(hw.range(TaggedSamp::bitwidth - 1, 0));
            held = true;
        }

        bool resolved = false;
        ap_uint<2> verdict = RF_TX_PLAYED;
        bool stale = false;

        // `now` means "the next available slot", and the PLAYER assigns it because the player is
        // the only thing that knows where slot is.  Consecutive `now` samples land on consecutive
        // slots for free: one sample is consumed per slot, so no base register is needed and there
        // is no START_NOW_LEAD to derive.  A constant you cannot derive is usually a design smell.
        // THE COMPARE IS ON THE CRITICAL PATH, so it is written as a raw signed difference and its
        // sign/zero tested directly, rather than as a `select` feeding a compare.  Measured (Vitis
        // 2025.1): the `select` between "compare" and "0 because now" cost 0.357 ns *inside* the
        // recurrence that gates the FIFO read enable.  Same three-way answer, one less level.
        ap_int<IDX_W> d = rf_tx_time_compare<IDX_W>(h.wr, slot);
        bool cmp_after = !h.now && (d > 0);        // AFTER
        bool cmp_before = !h.now && (d < 0);       // BEFORE; anything else is AT

        if (!held) {                               // nothing loaded: the slot is filled anyway
            samp_out.write(filler);
            n_underrun = n_underrun + 1;
        } else if (cmp_after) {                    // AFTER — not due yet; keep holding it
            samp_out.write(filler);
            n_underrun = n_underrun + 1;
        } else if (cmp_before) {                   // BEFORE — its slot has gone; discard, no emit
            held = false;
            resolved = h.request_status;
            verdict = RF_TX_MISSED;
            // NO write and NO slot++ on this path: the sample is stale, and the slot it missed was
            // already filled and already counted when it came due.  Advancing the slot here would
            // skip a live one to bury a dead one.
            stale = true;
        } else {                                   // AT — this is its slot (or `now`)
            samp_out.write(h.samp);
            played_through = slot;
            held = false;
            resolved = h.request_status;
            verdict = RF_TX_PLAYED;
        }

        if (resolved) {
            TxStatus s;
            s.slot = slot;
            s.verdict = verdict;
            s.played_through = played_through;
            s.n_underrun = n_underrun;
            ap_uint<TAG_W> sw = 0;
            sw.range(TxStatus::bitwidth - 1, 0) = TxStatus::pack_to_uint(s);
            if (!status_out.write_nb(sw)) {
                n_status_dropped = n_status_dropped + 1;
            }
        }
        // ONE exit test, at the bottom, rather than a `continue` in the middle.  Same behaviour --
        // a stale sample costs no slot -- but the status write above is then shared by both paths
        // instead of duplicated, and there is one place where `slot` advances.  A duplicated status
        // write is how the two paths drift.
        if (!stale) {
            slot = slot + 1;
        }
    }
}

#endif  // WAVEFLOW_RF_TX_PLAYER_TASK_H
