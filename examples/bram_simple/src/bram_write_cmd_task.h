#ifndef BRAM_SIMPLE_BRAM_WRITE_CMD_TASK_H
#define BRAM_SIMPLE_BRAM_WRITE_CMD_TASK_H
// bram_write_cmd_task — one command per firing, into a memory that lives OUTSIDE the kernel.
//
// Hand-written, and it stays hand-written for the reason mem_r_stream_task.h is: the body owns a
// resource the extractor has no vocabulary for -- there an `m_axi` pointer, here a `bram` array
// parameter.  Its Python twin (`BramWriteCmd.run_iter`) is the pysim golden, not the source of this
// file.
//
// THE MESSAGE IS READ IN ONE CALL, AND THAT IS THE POINT.
//
//     WriteCmd c;  c.read_stream<W>(cmd);
//
// never `ap_uint<W> wp = cmd.read();` twice.  `bram_write_cmd.h` is GENERATED from the Python
// `WriteCmd` DataList, so the field order and the widths have exactly one author.  Pulling the words
// out by hand would author the layout a second time, in the one place nothing checks it against the
// generated header -- the same defect as hand-rolled element packing, one level up.  The same holds
// on the way out: `r.write_stream<W>(resp)`.
//
// The PAYLOAD is different and is read a word at a time on purpose.  It is a data stream, not a
// structured message: there is no layout to agree about, and one word per beat is what an II=1 loop
// wants.
//
// THE RESPONSE IS THE WHOLE REASON THIS IS NOT A RELAY.  A write has no return path: a command that
// does not fully land completes silently and leaves the memory half-written.  One response per
// command is what makes a refusal visible, and `tid` is what lets a caller match it to the command
// it issued rather than to the order they came back in.
//
// A REFUSED COMMAND STILL CONSUMES ITS PAYLOAD.  The payload belongs to the command; leaving it in
// the stream would shift every later command's data by `nsamp` words and turn one caller error into
// a corrupted run.  Discarding costs the same cycles as writing and keeps the two streams in step.
//
// THE `go` TOKEN IS SENT ONCE, EVER.  It is what arms the reader (see bram_read_cmd_task.h), and one
// token is all the ordering this design needs: after it, the reader is command-driven and the two
// tasks are free to be live at the same time -- which is the point of a true-dual-port memory, and
// which makes keeping their ranges disjoint the CALLER's job rather than the design's.
//
// The `static bool` is safe here for a reason worth naming, because a `static` in an hls::task is
// otherwise the reset trap that cost examples/rf_blk_delay a day: a task that WRITES before it READS
// counts during reset.  This body's first statement is a blocking read of the command, so nothing it
// does can be counted before the design is running.
#include "hls_stream.h"
#include <ap_int.h>

#include "bram_cmd_range.h"
#include "bram_status.h"
#include "bram_write_cmd.h"
#include "bram_write_resp.h"

/// @tparam W  payload width in bits.  The message schemas carry one field per word at this width, so
///            `read_stream<W>` static_asserts on any W the schema was not generated for.
/// @tparam N  memory depth in words (the ARRAY SIZE: `mode=bram` on an unsized pointer silently
///            degrades to an ap_vld scalar port, so this is load-bearing, not decoration)
template <int W, int N>
void bram_write_cmd_task(ap_uint<W> buf_w[N], hls::stream<ap_uint<W> >& cmd,
                         hls::stream<ap_uint<W> >& data, hls::stream<ap_uint<W> >& resp,
                         hls::stream<ap_uint<W> >& go) {
    static bool announced = false;

    WriteCmd c;
    c.read_stream<W>(cmd);
    bool ok = bram_cmd_in_range<W, N>(c.waddr, c.nsamp);

write_payload:
    for (ap_uint<32> i = 0; i < c.nsamp; i++) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min = 1 max = N
        ap_uint<W> x = data.read();
        if (ok) buf_w[c.waddr + i] = x;   // refused: consumed, then dropped on the floor
    }

    WriteResp r;
    r.tid = c.tid;
    r.status = ok ? BramStatus::OK : BramStatus::OUT_OF_RANGE;
    r.write_stream<W>(resp);

    if (!announced) {
        go.write((ap_uint<W>)1);
        announced = true;
    }
}

#endif  // BRAM_SIMPLE_BRAM_WRITE_CMD_TASK_H
