#ifndef WAVEFLOW_RF_SAMP_BUF_PLAYER_TASK_H
#define WAVEFLOW_RF_SAMP_BUF_PLAYER_TASK_H
// rf_samp_buf_player_task.h — the TX sample buffer's PLAYER: one word out of the circular buffer,
// one word to the DAC, every slot, forever.  A single firing; the hls::task runtime re-fires it.
//
// THE NEVER-MISS-A-DEADLINE LAW APPLIES TO THIS TASK, AND ONLY TO THIS TASK.
//
// This is the mirror of rf_samp_buf_ingress_task.h and the asymmetry is the whole design.  An ADC
// cannot be back-pressured, so the RX ingress may never REFUSE A WRITE and fails by overrunning.  A
// DAC consumes a word every sample period whether or not one is ready for it, so this task may never
// MISS A DEADLINE and fails by underrunning.  Same shape, opposite failure.
//
// Three things follow, and they are what makes this body what it is:
//
//   * IT IS FREE-RUNNING, NOT DEMAND-DRIVEN.  It emits on the grid, always.  There is no command
//     here and nothing to wait for: the buffer is CIRCULAR, not a FIFO, and the play pointer walks
//     it forever.  "Nothing to send" is not a state this task can be in.
//   * IT NEVER BLOCKS WAITING FOR DATA.  If the loader has not filled the slot, this task emits what
//     is in the slot -- stale from the previous lap, or the buffer's initial contents -- and counts
//     an underrun.  Waiting for the loader is the one thing it may not do, exactly as the RX ingress
//     may not stall its input: the DAC's next sample period arrives regardless.
//   * THERE ARE NO DROPPED SAMPLES ON THE PLAYOUT SIDE.  Nothing is discarded; a slot is either
//     right or stale, and the counter is what tells them apart.
//
// The ONE blocking call is `s_out.write()`, and that is not waiting for data -- it is the DAC's own
// TREADY, which is the metronome this task runs on.  The DAC has a real input FIFO and does
// back-pressure the fabric; being paced by it is correct.  Being paced by the LOADER would not be.
//
// A WORD CARRIES SPW SAMPLES, so one firing delivers SPW samples and this task sustains
// SPW / fire_cycles samples per fabric cycle -- the same throughput lever as the RX ingress, and it
// has to be, because the two are sized against the same converter.
//
// WHY THE UNDERRUN COUNTER IS NOT AN RTL OUTPUT.  It has no port here, exactly as the RX capture's
// n_too_old has none: a per-firing counter would need a stream of its own and would be read once at
// the end of time.  In pysim it is a `@sim_only` counter; at RTL the same fact is observable where
// it belongs -- on the converter model, whose zero-fill/underrun counters are what an XSI run reads.
// A design that starves its DAC shows up there, which is the same place the RX drop count lives.
#include "hls_stream.h"
#include <ap_int.h>

/// @tparam W      AXIS word width in bits — SPW samples of W/SPW bits each.
/// @tparam SPW    samples per word.  MUST be a power of two: the sample->word conversion below is
///                then a shift, and a divide in the never-miss path costs cycles the DAC does not
///                give back.
/// @tparam N      buffer depth in WORDS.  MUST be a power of two: the wrap is a bit mask, and the
///                wrap is what makes this a circular buffer rather than a FIFO.
/// @tparam IDX_W  width of the SAMPLE-INDEX counter, and of every TxCmd/TxResp field.  Deliberately
///                independent of W, for the reason given in the ingress body.
template <int W, int SPW, int N, int IDX_W>
static void rf_samp_buf_player_task(ap_uint<W> buf_r[N], hls::stream<ap_uint<W> >& wr_in,
                                    hls::stream<ap_uint<W> >& rd_out,
                                    hls::stream<ap_uint<W> >& s_out) {
    // The play pointer, in SAMPLE INDEX — free-running and wrapping at 2^IDX_W.  It is not the
    // address: the address is the low log2(N) bits of `rd / SPW`.  Keeping the index rather than the
    // address is what lets the loader compare positions at all, and keeping it in SAMPLES rather
    // than words is what lets a host name the moment a sample plays.
    static ap_uint<IDX_W> rd = 0;
    // A LOWER bound on how far the loader has filled.  Polled non-blockingly: waiting on this
    // channel would be waiting for the loader, which is the one thing this task may not do.
    static ap_uint<IDX_W> last_wr = 0;

    ap_uint<W> w;
    if (wr_in.read_nb(w)) {
        last_wr = (ap_uint<IDX_W>)w;
    }
    // `rd - last_wr >= 0` means the play pointer has reached or passed everything known to be
    // filled, so this slot is stale.  The value is emitted anyway -- see the header.  (The counter
    // itself is pysim-side; there is no port for it here.)

    s_out.write(buf_r[(rd / SPW) & (N - 1)]);   // the ONE blocking call: the DAC's TREADY
    rd = rd + SPW;
    rd_out.write_nb((ap_uint<W>)rd);            // may fail; failing is correct, only the newest counts
}

#endif  // WAVEFLOW_RF_SAMP_BUF_PLAYER_TASK_H
