#ifndef WAVEFLOW_RF_CIRC_PLAY_TASK_H
#define WAVEFLOW_RF_CIRC_PLAY_TASK_H
// rf_circ_play_task.h — the repeat scheduler, IN FABRIC.  Load a waveform once, then replay it on a
// fixed period forever, through the ordinary TX path.
//
// WHY THIS IS NOT A TESTBENCH.  The behaviour is REACTIVE: it issues `start_now`, waits for the
// TxResp to learn where the waveform landed, and schedules every later play at
// `samp_start + k*PERIOD`.  A stimulus that depends on the DUT's own output cannot be written into a
// vector file before the run, so a file-driven AxisMaster cannot produce it.
//
// A reactive BFM was possible -- XsiSimObj is a cycle-accurate FSM interface and AxiMmReadSlave
// already reacts to the DUT -- and it was rejected for a DESIGN reason rather than a tooling one:
// no practical host could keep up.  A scheduler issuing a command every PERIOD samples at hundreds
// of MSa/s is fabric work.  It also avoids a second model, and two implementations of one behaviour
// that must agree is the trap this arc has paid for repeatedly.
//
// The testbench's whole job is now: push NSAMP words once.
//
// A TWO-PORT BRAM WOULD BE SIMPLER, AND THAT IS THE POINT.  For this behaviour alone, the PS writes
// a waveform into port A, the player reads port B at `slot % NSAMP`, and there is no command, no
// response and no scheduling -- a modulo counter and a memory.  But Example 1 exists to exercise the
// TX PATH, so it has to go through it: TxCmd in, in-band payload behind it, one TxResp per command,
// `start_now` resolved by the player, MAX_IN_FLIGHT bounding the pipe.  A simpler implementation
// would be a better product and a worse test.  Worth writing down, because the alternative is
// genuinely more elegant and someone will otherwise wonder why a repeat player needs acks.
//
// THE PRIVATE ARRAY IS NOT THE BRAM THIS REDESIGN REMOVED.  That was a BRAM *shared between two
// tasks*, which has no handshake, which forced the out-of-band progress channel, whose
// data-dependent spin is what Vitis will not pipeline.  `wave[]` is local storage inside one task:
// nothing else reads it, there is no channel, no progress pointer, no MARGIN and no spin.  It lowers
// to a BRAM with no dataflow semantics attached.
//
// THE SIZING RULE, DERIVED AND THEN MEASURED.
//
//     LEAD  >=  ceil( (T_load + NSAMP - 1) / PERIOD )
//
// where T_load is the latency from *the response that frees a slot* to *the last payload word of
// the command it releases*, in DAC slots.  The window available for that work is
// `LEAD*PERIOD - NSAMP + 1` slots: the response for play k arrives when its last sample plays, at
// slot `base + k*PERIOD + NSAMP - 1`, and the command it releases (play k+LEAD) is due at slot
// `base + (k+LEAD)*PERIOD`.
//
// MEASURED at RTL (2026-08-21, VCD of the internal channels, xczu48dr @ 250 MHz, 64 MSa/s):
//
//     response -> last payload word   74 cycles median (18.9 slots), 150 worst (38.4 slots)
//     window at LEAD = 2              65 slots (254 cycles)
//     margin                          3.43x median, 1.69x worst
//     minimum LEAD from the measured T_load   ceil((18.9 + 63)/64) = 2
//
// So LEAD = 2 is not a round number that happened to work: it is the SMALLEST value this geometry
// admits, and the measurement says so.  At LEAD = 1 the window is `PERIOD - NSAMP + 1` = ONE slot
// against a need of ~19 -- short by a factor of 19, which is why back-to-back replay cannot be
// served by a scheduler that blocks on each response.
//
// The framing overhead does NOT eat the slack at LEAD = 2; that hypothesis was checked and refuted
// with the numbers above.  What DID look like slack exhaustion was the `k = 1` bug below.
//
// THE RESET TRAP APPLIES.  This body WRITES (a command) before it has read anything blocking on the
// FIRST path.  `#pragma HLS reset` in the body does not close it under Vitis 2025.1 -- measured;
// what does is `config_rtl -reset state` at the solution level, which the generated tcl carries.
#include "hls_stream.h"
#include <ap_int.h>
#include "rf_tx_cmd.h"
#include "rf_tx_resp.h"

#ifndef RF_TX_TRANSMITTED
#define RF_TX_TRANSMITTED 0
#define RF_TX_TOO_LATE    1
#define RF_TX_MISALIGNED  2
#define RF_TX_NO_SLOT     3
#define RF_TX_ZERO_LEN    4
#endif

//: The three states.  A replacement waveform returns to FIRST, never to REPEAT — it must re-learn
//: "now", because the old `base` describes a schedule the new waveform was never part of.
#define RF_CIRC_LOAD   0
#define RF_CIRC_FIRST  1
#define RF_CIRC_REPEAT 2

/// @tparam W       AXIS word width in bits — one sample per word at the gated geometry.
/// @tparam NSAMP   samples in one play.  The private array's depth.
/// @tparam PERIOD  slots between successive play STARTS.  `== NSAMP` is back-to-back replay.
/// @tparam LEAD    commands kept outstanding.  See the header — 2, and derived.
/// @tparam POLLS   responses harvested per pass.  BOUNDED and compile-time (rule 3): this unrolls
///                 into POLLS `read_nb` calls.  `while (got)` is the data-dependent trip count that
///                 costs the old design its II.
/// @tparam IDX_W   width of the slot counter and of every TxCmd/TxResp index field.
template <int W, int NSAMP, int PERIOD, int LEAD, int POLLS, int IDX_W>
static void rf_circ_play_task(hls::stream<ap_uint<W> >& wave_in,
                              hls::stream<ap_uint<W> >& cmd_out,
                              hls::stream<ap_uint<W> >& samp_out,
                              hls::stream<ap_uint<W> >& resp_in) {
    static ap_uint<W> wave[NSAMP];
#pragma HLS bind_storage variable=wave type=RAM_2P impl=bram
    static ap_uint<2> state = RF_CIRC_LOAD;
#pragma HLS reset variable=state
    static ap_uint<IDX_W> base = 0;        // where play 0 actually landed
#pragma HLS reset variable=base
    static ap_uint<IDX_W> k = 0;           // which repeat
#pragma HLS reset variable=k
    static ap_uint<IDX_W> tid = 0;
#pragma HLS reset variable=tid
    static ap_uint<8> outstanding = 0;
#pragma HLS reset variable=outstanding
    static ap_uint<IDX_W> n_played = 0, n_late = 0, n_no_slot = 0, n_reloads = 0;
#pragma HLS reset variable=n_played
#pragma HLS reset variable=n_late
#pragma HLS reset variable=n_no_slot
#pragma HLS reset variable=n_reloads

    while (1) {
        if (state == RF_CIRC_LOAD) {
            for (int i = 0; i < NSAMP; i++) {
#pragma HLS PIPELINE II=1
                wave[i] = wave_in.read();          // blocking: nothing is playing yet
            }
            n_reloads = n_reloads + 1;
            state = RF_CIRC_FIRST;
        }

        else if (state == RF_CIRC_FIRST) {
            // start_now: the PLAYER assigns the slots, and the response reports where they went.
            // The ONLY way to learn "now" -- and the reason there is no zero-length probe command,
            // which has no last sample to mark, so no status returns and the pending slot leaks.
            {
                TxCmd c;
                c.tid = tid;
                c.samp_start = 0;                  // ignored: the player assigns it
                c.start_now = 1;
                c.nsamp = NSAMP;
                c.write_stream<W>(cmd_out);
                tid = tid + 1;
                for (int i = 0; i < NSAMP; i++) {
#pragma HLS PIPELINE II=1
                    samp_out.write(wave[i]);       // in-band payload, behind the command
                }
            }
            TxResp r;
            r.read_stream<W>(resp_in);             // blocking HERE is correct: nothing else to do
            base = r.samp_start;
            // THE TRAIN STARTS AT k = LEAD, NOT k = 1, and it is forced rather than chosen.  The
            // response for the start_now window arrives when its LAST sample plays -- i.e. at slot
            // `base + NSAMP`, which at back-to-back replay (PERIOD == NSAMP) IS slot
            // `base + 1*PERIOD`.  Play 1 is therefore ALREADY DUE at the moment this task first
            // learns where play 0 landed, and the command for it cannot be loaded until after that.
            //
            // MEASURED at RTL with `k = 1` (the value this line held until 2026-08-21): play 1's
            // first EIGHT samples arrived after their slots had gone, took the player's BEFORE path,
            // and were discarded -- 8 zero-filled DAC slots, then the window resumed mid-waveform at
            // position 8.  Every later play was then exactly PERIOD apart, so it read as a constant
            // phase error rather than as the one-off it is.
            //
            // `LEAD` is the right constant because it was derived from the same relationship: it is
            // how many periods of work this task keeps in flight, so it is also how far ahead the
            // first schedulable slot is.  The Python twin has always had it; this line was the
            // divergence.
            k = LEAD;
            if (r.status == RF_TX_TRANSMITTED) n_played = n_played + 1; else n_late = n_late + 1;
            state = RF_CIRC_REPEAT;
        }

        else {                                     // RF_CIRC_REPEAT
            // A replacement waveform, checked WITHOUT blocking -- a blocking check would stall the
            // schedule waiting for a waveform that may never come.
            if (!wave_in.empty()) {
                state = RF_CIRC_LOAD;
                continue;
            }

            // BOUNDED (rule 3).  Deliberately not called `harvest`: TxLoader::harvest pops a pending
            // FIFO and maintains a correspondence; this counts responses and frees slots.
            for (int i = 0; i < POLLS; i++) {
#pragma HLS PIPELINE II=1
                if (resp_in.empty()) break;
                TxResp r;
                r.read_stream<W>(resp_in);
                // No guard: the loader guarantees ONE response per accepted command, so this cannot
                // underflow.  If it ever did, that guarantee had already broken and a counter is the
                // wrong place to find out.  The pysim twin asserts it, because pysim is where an
                // invariant is cheap to check.
                outstanding = outstanding - 1;
                if (r.status == RF_TX_TRANSMITTED)   n_played  = n_played + 1;
                else if (r.status == RF_TX_NO_SLOT)  n_no_slot = n_no_slot + 1;
                else                                 n_late    = n_late + 1;
            }

            if (outstanding < LEAD) {
                TxCmd c;
                c.tid = tid;
                c.samp_start = (ap_uint<IDX_W>)(base + k * PERIOD);   // ABSOLUTE, never relative
                c.start_now = 0;
                c.nsamp = NSAMP;
                c.write_stream<W>(cmd_out);
                tid = tid + 1;
                for (int i = 0; i < NSAMP; i++) {
#pragma HLS PIPELINE II=1
                    samp_out.write(wave[i]);
                }
                outstanding = outstanding + 1;
                k = k + 1;
            }
        }
    }
}

#endif  // WAVEFLOW_RF_CIRC_PLAY_TASK_H
