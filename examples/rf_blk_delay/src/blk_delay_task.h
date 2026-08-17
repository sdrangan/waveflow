#ifndef WAVEFLOW_BLK_DELAY_TASK_H
#define WAVEFLOW_BLK_DELAY_TASK_H
// blk_delay_task.h — the pattern-B user block: move block k from the RX sample buffer into slot
// k + DELAY of the TX sample buffer.  One firing is one block; the hls::task runtime re-fires.
//
// THIS BODY IS THE ARGUMENT FOR PATTERN B, AND ITS LENGTH IS THE ARGUMENT.
//
// Compare rf_samp_pass_through_task.h, which touches the converter boundary directly: that one had
// to be split into two tasks over a sized FIFO, had to write one word per firing so the boundary
// stage could never stall, and still needed a hand-written word-granular body to stop dropping
// samples.  None of that is here.  This task may BLOCK freely on all three of its streams -- nothing
// downstream misses a deadline while it waits, because the player next door keeps playing and the
// ingress next door keeps filling.  The never-stall obligation moved into RfSampBuf, once.
//
// WHY THE COMMAND GOES OUT BEFORE A SINGLE SAMPLE HAS ARRIVED.
//
// The TxCmd naming the destination is written to s_out first, and only then are the block's words
// relayed one at a time.  That is what makes this a RELAY rather than a copy: there is no block
// storage in this task, and at BLK = 256 the buffer it would otherwise need is the size of the ones
// either side of it.  The loader reads the command, then NPAY words behind it -- the same in-band
// framing rf_samp_buf_loader_task.h documents -- so committing to the destination early costs
// nothing and saves the storage.
//
// THE DELAY IS IN BLOCKS, AND THAT IS A CONTRACT, NOT A CONVENIENCE.
//
// rf_samp_buf_loader_task.h refuses a window that is not a whole number of words
// (RF_SAMP_BUF_MISALIGNED), because a sub-word window would mean unpacking, selecting and re-packing
// inside a loop that must stay cheap.  BLK is a multiple of SPW by construction, so `in_ts` and
// `out_ts = in_ts + DELAY*BLK` are both word-aligned for free and this task never has to think
// about it.
//
// NO BLOCK COUNT.  The Python model carries an `n_blk` so SimPy's queue can empty and the run can
// end; hardware needs no such bound and is not given one.  This task idles the way an hls::task with
// nothing to do always idles: it asks for the next block and blocks reading a payload the ADC has
// not produced.
#include "hls_stream.h"
#include <ap_int.h>
#include "rx_cmd.h"
#include "tx_cmd.h"

/// @tparam W      AXIS word width in bits
/// @tparam SPW    samples per word (power of two)
/// @tparam BLK    samples in one block; a multiple of SPW
/// @tparam DELAY  the delay, in BLOCKS
/// @tparam IDX_W  width of the sample-index counter and of every RxCmd/TxCmd field
template <int W, int SPW, int BLK, int DELAY, int IDX_W>
static void blk_delay_task(hls::stream<ap_uint<W> >& s_cmd,
                           hls::stream<ap_uint<W> >& s_in,
                           hls::stream<ap_uint<W> >& s_out) {
    // The only state this task keeps: which block it is on.  It wraps with the sample index it
    // drives, at 2^IDX_W, exactly as the buffers' pointers do.
    static ap_uint<IDX_W> k = 0;
    // ...AND IT MUST BE RESET EXPLICITLY.  This is not defensive; without it the design hangs.
    //
    // THE LAW: an hls::task that WRITES BEFORE IT READS advances its state during reset.
    //
    // A static's `= 0` becomes a simulation initial value (`#0 k = 16'd0;` in the RTL), not a reset.
    // Whether that matters depends on whether anything holds the task still while reset is asserted,
    // and for every other task in this repo something does: the ingress, the capture and the loader
    // all BEGIN with a blocking stream read, so their first state is empty, they stall, and their
    // counters do not move.  This task begins by WRITING a command -- it has to, it is the one that
    // initiates -- and an empty output FIFO does not block anybody.  So its update
    //
    //     always @(posedge ap_clk) if (state1 && !blocked) k <= k + 1;   // no ap_rst term
    //
    // fires on every cycle of reset.  The XSI harness holds reset 16 cycles (xsi_bfm.h Dut::reset),
    // so the design came out of reset at k = 16 and asked the RX buffer for sample 16*256 = 4096 --
    // a window the ADC, which only ever produces 3072 samples, never reaches.  The capture waited
    // for it forever and the whole loop was silent: zero responses from either buffer, while the ADC
    // reported dropping nothing.  Nothing in pysim can see this: SimPy has no reset.
    //
    // `#pragma HLS reset` puts the ap_rst term back.  Diagnosed by bisecting the body at RTL --
    // with a constant `rc.start = 0` the loop ran, with `k * BLK` it did not, and that difference is
    // what pointed here rather than at the buffers.
#pragma HLS reset variable=k

    const ap_uint<IDX_W> in_ts = k * BLK;
    const ap_uint<IDX_W> out_ts = in_ts + (ap_uint<IDX_W>)(DELAY * BLK);

    // 1. Ask the RX buffer for block k BY SAMPLE INDEX -- not by a buffer address.  The capture
    //    blocks per word until the ADC has produced them, so this loop paces itself off the
    //    converter without ever naming a rate.
    RxCmd rc;
    rc.tid = k + 1;
    rc.start = in_ts;
    rc.nsamp = BLK;
    rc.write_stream<W>(s_cmd);

    // 2. Commit to the destination, then relay.  out_ts = in_ts + DELAY*BLK is the whole of what
    //    this module computes, and it is RfSampBuf's contract written as one line of arithmetic.
    TxCmd tc;
    tc.tid = k + 1;
    tc.start = out_ts;
    tc.nsamp = BLK;
    tc.write_stream<W>(s_out);

    for (int i = 0; i < BLK / SPW; i = i + 1) {
#pragma HLS PIPELINE II=1
        s_out.write(s_in.read());
    }

    k = k + 1;
}

#endif  // WAVEFLOW_BLK_DELAY_TASK_H
