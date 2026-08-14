#include "hls_task.h"
#include "hls_stream.h"
#include <ap_int.h>

#define DEPTH 1024

// Task A: writes the buffer at a running pointer.  Never reads it.
static void write_task(ap_int<16> buf_w[DEPTH], hls::stream<ap_int<16> >& in) {
    static ap_uint<10> wr = 0;
    buf_w[wr] = in.read();
    ++wr;                                  // wraps at 1024 by width
}

// Task B: reads the buffer at an address it is TOLD.  Never writes it.
// Address-driven on purpose: it makes the test check that A's writes are visible to B,
// rather than racing a free-running read pointer past a write pointer.
static void read_task(ap_int<16> buf_r[DEPTH], hls::stream<ap_uint<16> >& addr,
                      hls::stream<ap_int<16> >& out) {
    ap_uint<16> a = addr.read();
    out.write(buf_r[a]);
}

void rx(ap_int<16> buf_w[DEPTH], ap_int<16> buf_r[DEPTH],
        hls::stream<ap_int<16> >& rx_str, hls::stream<ap_uint<16> >& addr_str,
        hls::stream<ap_int<16> >& out_str) {
#pragma HLS INTERFACE mode=bram port=buf_w storage_type=ram_1wnr latency=1
#pragma HLS INTERFACE mode=bram port=buf_r storage_type=ram_1wnr latency=1
#pragma HLS INTERFACE axis port=rx_str
#pragma HLS INTERFACE axis port=addr_str
#pragma HLS INTERFACE axis port=out_str
#pragma HLS INTERFACE ap_ctrl_none port=return
    hls_thread_local hls::task t0(write_task, buf_w, rx_str);
    hls_thread_local hls::task t1(read_task,  buf_r, addr_str, out_str);
}
