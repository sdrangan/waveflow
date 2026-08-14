#ifndef BRAM_TOY_BRAM_READ_TASK_H
#define BRAM_TOY_BRAM_READ_TASK_H
// bram_read_task — answer one address per firing, from a memory the WRITER also holds open.
//
// The witness's `read_task` (plans/witness/t2p_bram/rx_kernel.cpp), plus a one-time arm on `go`.
//
// Why the arming exists.  The witness sequenced its two tasks from the testbench: tb.v drove all
// 256 samples, and only then the addresses.  A concurrent BFM harness cannot do that — both
// AxisMasters push from cycle 0 — so a read of address 255 would otherwise land hundreds of cycles
// before that word was written.  The answer belongs in the DESIGN, not in the testbench: the reader
// waits once for the writer's "buffer ready" token and is address-driven from then on.
//
// It is also the invariant the memory asserts.  `bram_t2p.v` $errors when port B reads the address
// port A is writing that cycle; "rd trails wr" is exactly what this token establishes, and a run
// that completes without that $error firing is evidence the ordering held.
#include "hls_stream.h"
#include <ap_int.h>

/// @tparam W  payload width in bits
/// @tparam N  memory depth in words (the array SIZE — see bram_write_task.h)
template <int W, int N>
void bram_read_task(ap_uint<W> buf_r[N], hls::stream<ap_uint<W> >& go,
                    hls::stream<ap_uint<W> >& addr, hls::stream<ap_uint<W> >& out) {
    // Persists across firings: the wait happens on the first firing and never again, so the
    // steady-state cost of the arming is nothing.
    static bool armed = false;
    if (!armed) {
        (void)go.read();          // blocks until the writer has filled the buffer
        armed = true;
    }

    ap_uint<W> a = addr.read();
    out.write(buf_r[a]);
}

#endif  // BRAM_TOY_BRAM_READ_TASK_H
