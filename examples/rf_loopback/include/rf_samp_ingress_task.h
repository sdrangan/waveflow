#ifndef WAVEFLOW_RF_SAMP_INGRESS_TASK_H
#define WAVEFLOW_RF_SAMP_INGRESS_TASK_H
// rf_samp_ingress_task.h — the FIXED ingress body: one word off the AXIS boundary port, one word
// into the internal FIFO.  A single firing; the hls::task runtime re-fires it, so there is no loop
// here.  Copied verbatim into the kernel's include dir by rf_dut_build.gen_headers and instantiated
// at a concrete width by the generated top (rf_samp_ingress_task<64>).
//
// WHY THIS TASK EXISTS
//
// A converter cannot be back-pressured.  The ADC presents a beat every ~4.7 cycles and whatever the
// fabric is not ready for is gone — so no stage that touches the boundary port may stop reading it.
// The block stage behind this one *must* stop reading (it holds a whole block and writes it out in a
// contiguous burst), and when it was the only stage, 72 of 512 words were dropped at RTL.
//
// Nothing about that is fixed by a deeper port: a depth pragma on a top-level argument is ignored by
// Vitis (HLS 214-387), so a boundary port is 2 deep whatever the Python says.  The elastic buffer has
// to be an INTERNAL channel, and an internal channel needs a task to fill it.  That is this file.
//
// WHY IT IS HAND-WRITTEN
//
// Its Python twin (RfSampPassThrough's RfSampIngress.run_iter) relays a whole BURST, because a burst
// is pysim's quantum: StreamIFSlave.get pops one burst and truncates it to the requested width, so a
// word-granular Python body would silently discard 63 of every 64 words.  The relay the hardware
// needs therefore has no pysim expression.  The two are declared separately and are identical at
// block granularity, which is the only granularity pysim resolves — see docs/guide/rf/fidelity.md.
#include "hls_stream.h"
#include <ap_int.h>

template <int W>
static void rf_samp_ingress_task(hls::stream<ap_uint<W> >& s_in,
                                 hls::stream<ap_uint<W> >& w_out) {
    w_out.write(s_in.read());
}

#endif  // WAVEFLOW_RF_SAMP_INGRESS_TASK_H
