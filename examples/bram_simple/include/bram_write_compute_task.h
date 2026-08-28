#ifndef BRAM_SIMPLE_BRAM_WRITE_COMPUTE_TASK_H
#define BRAM_SIMPLE_BRAM_WRITE_COMPUTE_TASK_H
// bram_write_compute_task — one command per firing, TWO opcodes, against a memory that lives
// OUTSIDE the kernel.
//
// Hand-written, and it stays hand-written for the reason mem_r_stream_task.h is: the body owns a
// resource the extractor has no vocabulary for -- there an `m_axi` pointer, here a `bram` array
// parameter.  The COMPUTE branch adds a second reason of the same kind: its Python twin is
// `array_ref`, a live view of the memory, and a view has NO HLS lowering -- in C++ the port simply
// IS the array, and reading and writing it through one subscript is what "in place" means.  So the
// two twins are not the same code and were never going to be: `BramWriteCompute.run_iter` is the
// pysim golden, this file is the hardware.
//
// THE PORT IS READ-WRITE, AND THAT IS WHY THE COMPUTE LOOP RUNS AT II=2.
//
// `buf_w` is declared `access="readwrite"` in Python, so its pragma carries
// `storage_type=ram_1p` rather than `ram_1wnr`.  The wrapper wires ONE physical memory port per
// declared `bram` port, and `ram_1wnr` would let Vitis hit II=1 on the loop below by reading on
// port B while writing on port A -- a port the wrapper never wired, so those reads would return X
// or stale data with a clean csynth and nothing visible until RTL (plans/typed_transfer_codec.md
// S5b).  `ram_1p` does not declare a B half at all.  One port, two accesses per element, II=2.
//
// That number is the LESSON, not a limitation to work around.  "In place is II=2" is false in
// general; "the wrapper gives you one physical port, so the pragma pins Vitis to one, so
// read-modify-write costs two cycles per element" is true and explains itself.
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
// A REFUSED *WRITE* STILL CONSUMES ITS PAYLOAD.  The payload belongs to the command; leaving it in
// the stream would shift every later command's data by `nsamp` words and turn one caller error into
// a corrupted run.  Discarding costs the same cycles as writing and keeps the two streams in step.
//
// A COMPUTE CONSUMES NO PAYLOAD -- refused or not.  It reads the words it is about to rewrite, so
// there is nothing on `data` that belongs to it, and reading one would desynchronize the stream just
// as badly in the other direction.  The payload loop is therefore INSIDE the WRITE branch, and the
// scenario writer frames `data_w` against the WRITE commands only.
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
#include "bram_op.h"
#include "bram_status.h"
#include "bram_write_compute_cmd.h"
#include "bram_write_resp.h"

/// @tparam W  payload width in bits.  The message schemas carry one field per word at this width, so
///            `read_stream<W>` static_asserts on any W the schema was not generated for.
/// @tparam N  memory depth in words (the ARRAY SIZE: `mode=bram` on an unsized pointer silently
///            degrades to an ap_vld scalar port, so this is load-bearing, not decoration)
template <int W, int N>
void bram_write_compute_task(ap_uint<W> buf_w[N], hls::stream<ap_uint<W> >& cmd,
                             hls::stream<ap_uint<W> >& data, hls::stream<ap_uint<W> >& resp,
                             hls::stream<ap_uint<W> >& go) {
    static bool announced = false;

    WriteComputeCmd c;
    c.read_stream<W>(cmd);
    bool ok = bram_cmd_in_range<W, N>(c.waddr, c.nsamp);

    // The opcode is compared against a NAME, not against a bare integer, because `bram_op.h` is
    // generated from the same Python `BramOp` the model dispatches on.  A literal here would author
    // the encoding a second time in the one place nothing checks it against the schema.
    if (c.opcode == BramOp::WRITE) {
write_payload:
        for (ap_uint<32> i = 0; i < c.nsamp; i++) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min = 1 max = N
            ap_uint<W> x = data.read();
            if (ok) buf_w[c.waddr + i] = x;   // refused: consumed, then dropped on the floor
        }
    } else if (ok) {
        // No `data.read()` anywhere in here -- see the payload note in the header comment.  The
        // refused case skips the loop entirely rather than running it to drain something: there is
        // nothing to drain, and running it would write the memory a refusal promised not to touch.
        //
        // `x*3 + 1`, not `x + 1`: over a ramp an off-by-one in the address still increments
        // correctly, so `x + 1` would pass with the wrong words.  Vitis schedules this at II=2 --
        // one port, a read and a write per element.
compute_inplace:
        for (ap_uint<32> i = 0; i < c.nsamp; i++) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min = 1 max = N
            buf_w[c.waddr + i] = buf_w[c.waddr + i] * 3 + 1;
        }
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

#endif  // BRAM_SIMPLE_BRAM_WRITE_COMPUTE_TASK_H
