#ifndef WAVEFLOW_VEC_MULT_TASK_H
#define WAVEFLOW_VEC_MULT_TASK_H
// vec_mult_task.h — VecMult's hand-written body: buffer x, then stream y against it.
//
// One firing reads [ cmd(tx_id, n) | x_0..x_{n-1} | y_0..y_{n-1} ] off a SINGLE stream and writes
// [ z_0..z_{n-1} | resp(tx_id) ].
//
// WHY THE RESPONSE.  Emitting only z says nothing about WHICH job finished, so a caller with more
// than one request in flight cannot correlate a result with its command, and cannot distinguish a
// slow job from a lost one.  Echoing the transaction id makes completion observable rather than
// inferred from word counts.
//
// WHY THE BUFFER.  x and y share one port, so they arrive one after the other and cannot be
// consumed in the same beat.  The kernel has to hold x while y streams past.  That is a property of
// the INTERFACE, not of the arithmetic: with two separate input streams the reads would be
// independent ports, would schedule in the same beat, and no buffer would be needed at all
// (measured: II=1, BRAM=0).  A shared port is what makes storage unavoidable.
//
// WHY THE PARTITION.  The MULT beat consumes LW samples of buf at once.  An unpartitioned array is
// a 2-port memory, so more than two lanes would serialize and II would rise.  `cyclic factor=LW`
// puts element i in bank (i % LW), and because lane j of word i is element i*LW+j, lane j always
// reads bank j -- LW conflict-free accesses per cycle.
//
// That pragma is the entire reason this example exists: it converts a *throughput* requirement into
// a *BRAM* cost, and the cost is a ceiling rather than a ratio.  Each of the LW banks is VLEN/LW
// deep, and a bank shallower than one BRAM18 still occupies a whole one.  Note VLEN, not n: the
// buffer is sized by the COMPILE-TIME bound, so the area is paid whatever length actually arrives.
// See docs/guide/resource_model/example.md for the measured regimes.
//
// Word<->element packing is the GENERATED serializer (vm_au::read_stream_lane / write_stream_lane),
// never a hand-rolled .range() -- see guide/vectorization/hls/arrayutils.md.  Its pysim twin is
// VecMult.run_iter.
#include "hls_stream.h"
#include <ap_int.h>
#include "vecmult_types.h"
#include "vec_cmd.h"
#include "vec_resp.h"

template <int DWID, int VLEN>
static void vec_mult_task(hls::stream<ap_uint<DWID> >& s_in,
                          hls::stream<ap_uint<DWID> >& z_out) {
    typedef vm_au::value_type samp_t;
    const int LW = vm_au::lane_capacity<DWID>();
    // Taken FROM the sample type rather than written as a literal, so a change to value_type cannot
    // leave a stale width behind in the product below.
    const int SAMP_W = samp_t::width;

    // The command: a plain struct on a plain stream, not a framed descriptor -- nothing relays this
    // stream, so there is no payload anyone downstream must forward unparsed.
    VecCmd cmd;
    cmd.read_stream<DWID>(s_in);
    const int n = (int)cmd.n;

    // Firing-local, not cross-firing state: filled in LOAD and fully consumed in MULT, so nothing
    // has to survive the firing and this is NOT an add_state declaration.
    samp_t buf[VLEN];
#pragma HLS ARRAY_PARTITION variable=buf cyclic factor=LW dim=1

    samp_t xlane[LW], ylane[LW], zlane[LW];
#pragma HLS ARRAY_PARTITION variable=xlane complete dim=1
#pragma HLS ARRAY_PARTITION variable=ylane complete dim=1
#pragma HLS ARRAY_PARTITION variable=zlane complete dim=1

LOAD:
    for (int i = 0; i < n; i += LW) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT max=VLEN
        const int nlane = (n - i < LW) ? (n - i) : LW;   // ragged final beat
        vm_au::read_stream_lane<DWID>(s_in, xlane, nlane);
        for (int j = 0; j < LW; ++j) {
#pragma HLS UNROLL
            if (j < nlane) buf[i + j] = xlane[j];
        }
    }

MULT:
    for (int i = 0; i < n; i += LW) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT max=VLEN
        const int nlane = (n - i < LW) ? (n - i) : LW;
        vm_au::read_stream_lane<DWID>(s_in, ylane, nlane);
        for (int j = 0; j < LW; ++j) {
#pragma HLS UNROLL
            // No widening cast on the operands: ap_int multiply already returns W1+W2 bits, so
            // ap_int<16> * ap_int<16> IS ap_int<32> and the product is exact by construction.
            // Casting the operands up to 32 first would make the product ap_int<64> and push a
            // silent 32-bit truncation into this assignment -- harmless at SAMP_W=16 and wrong the
            // moment it changes.  The ONE cast that carries a decision is the narrowing one below.
            ap_int<2 * SAMP_W> p = buf[i + j] * ylane[j];
            // Wrapping product in the sample's own width -- the golden is numpy int16 arithmetic,
            // so the C++ must truncate identically rather than saturate.
            zlane[j] = (samp_t)p;
        }
        vm_au::write_stream_lane<DWID>(zlane, z_out, nlane);
    }

    // The response closes the transaction: same id in, same id out.
    VecResp resp;
    resp.tx_id = cmd.tx_id;
    resp.write_stream<DWID>(z_out);
}

#endif  // WAVEFLOW_VEC_MULT_TASK_H
