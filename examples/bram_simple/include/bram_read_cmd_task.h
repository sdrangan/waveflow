#ifndef BRAM_SIMPLE_BRAM_READ_CMD_TASK_H
#define BRAM_SIMPLE_BRAM_READ_CMD_TASK_H
// bram_read_cmd_task — one command per firing, out of a memory the WRITER also holds open.
//
// Its Python twin is `BramReadCmd.run_iter`, which is the pysim golden and NOT the source of this
// file.
//
// THE MESSAGE IS READ IN ONE CALL:  ReadCmd c;  c.read_stream<W>(cmd);
//
// `bram_read_cmd.h` is GENERATED from the Python `ReadCmd` DataList.  Reading the fields with N
// separate `cmd.read()` calls would state the field order and the widths a second time, in the one
// place nothing checks them against that header -- and the two would then be free to disagree
// silently.  The response goes out the same way, `r.write_stream<W>(resp)`.
//
// The PAYLOAD is written a word at a time, deliberately: it is a data stream rather than a
// structured message, so there is no layout to agree about.
//
// WHY A READ ANSWERS AT ALL.  A refused read returns zero words, and zero words is
// indistinguishable from "not yet" on a stream: a consumer waiting for `nsamp` words that will never
// arrive does not see an error, it sees a stream that has gone quiet.  So the only channel that can
// report a refusal is one that answers whether or not there is data -- which is precisely what the
// data stream cannot be.  `tid` is echoed so the answer can be matched to its command.
//
// THE ARMING, AND WHY IT IS ONCE.
//
// The witness (plans/witness/t2p_bram/) sequenced its two tasks from its testbench: tb.v drove all
// 256 samples and only then the addresses.  A concurrent BFM harness cannot do that -- every driver
// pushes from cycle 0 -- so the ordering belongs in the DESIGN.  One token, consumed once, is all of
// it: after that the reader is command-driven, and the two tasks are free to be live at the same
// time.  That freedom is the point of a true-dual-port memory, and it is also what makes keeping the
// ranges disjoint the caller's job.
//
// Hoisting the arm OUT of the per-word loop is not a micro-optimisation.  A conditional blocking
// read inside a pipelined body is a data-dependent stall, which Vitis reports as
//
//     [HLS 200-878] Unable to schedule the loop exit test ... (II = 1)
//
// and which is what pins the streaming buffer's bodies at II=2.  Here the question ("has anything
// been written yet?") is about the whole run rather than about this word, so it can be asked once.
//
// THE READ LATENCY IS NOT IN THIS FILE, AND MUST NOT BE.  `bram_t2p.v` publishes
// `localparam READ_LATENCY`, and the kernel's `latency=` pragma is emitted from that same number by
// composite_gen -- reached through the bound BramIF.  A second copy here would be free to disagree,
// and a disagreement shifts every returned word by one, silently.  That is what the ramp catches.
#include "hls_stream.h"
#include <ap_int.h>

#include "bram_cmd_range.h"
#include "bram_read_cmd.h"
#include "bram_read_resp.h"
#include "bram_status.h"

/// @tparam W  payload width in bits (see bram_write_cmd_task.h on why the schemas pin it)
/// @tparam N  memory depth in words (the ARRAY SIZE -- see bram_write_cmd_task.h)
template <int W, int N>
void bram_read_cmd_task(ap_uint<W> buf_r[N], hls::stream<ap_uint<W> >& go,
                        hls::stream<ap_uint<W> >& cmd, hls::stream<ap_uint<W> >& data,
                        hls::stream<ap_uint<W> >& resp) {
    // Persists across firings: the wait happens on the first firing and never again, so the
    // steady-state cost of the arming is nothing.  A `static` is safe here because this body READS
    // before it writes -- see bram_write_cmd_task.h on the hls::task reset trap.
    static bool armed = false;
    if (!armed) {
        (void)go.read();          // blocks until the writer has completed its first command
        armed = true;
    }

    ReadCmd c;
    c.read_stream<W>(cmd);
    bool ok = bram_cmd_in_range<W, N>(c.raddr, c.nsamp);

    if (ok) {
    read_payload:
        for (ap_uint<32> i = 0; i < c.nsamp; i++) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT min = 1 max = N
            data.write(buf_r[c.raddr + i]);
        }
    }

    ReadResp r;
    r.tid = c.tid;
    r.status = ok ? BramStatus::OK : BramStatus::OUT_OF_RANGE;
    r.write_stream<W>(resp);
}

#endif  // BRAM_SIMPLE_BRAM_READ_CMD_TASK_H
