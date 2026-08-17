// task_loop.cpp — what does a loop INSIDE an hls::task body cost?
//
// Hand-written, no Waveflow involvement.  The point of a witness is that it measures VITIS: if this
// file imported anything of ours, a surprising number could always be blamed on our generator.
//
// EIGHT TOPS: two body shapes x four loop shapes.  The two body shapes are the two real ones in
// waveflow/hw/rf_samp_buf*.py, reduced to their essential work:
//
//   ING  stream-read -> BRAM-write   (RfSampBufIngress, fire_cycles = 2 today)
//   PLY  BRAM-read   -> stream-write (RfSampBufPlayer,  fire_cycles = 3 today -- the BINDING one)
//
// and the four loop shapes are:
//
//   _1    one word per firing, no loop        the shape shipped today; the baseline
//   _n8   bounded loop, N = 8,  PIPELINE II=1
//   _n64  bounded loop, N = 64, PIPELINE II=1 (one block at the real geometry)
//   _w    while (1),            PIPELINE II=1
//
// TWO VALUES OF N ON PURPOSE.  With one, the boundary cost and the per-word cost cannot be
// separated: any (period, N) pair is consistent with infinitely many splits.  With two, the
// boundary falls out of the difference and the amortization is measured rather than asserted.
//
// The bodies are deliberately MINIMAL and IDENTICAL across loop shapes.  The shipped bodies also
// poll or post a progress channel; that work is real but it is the same in every shape, so
// including it would add a constant to every row and change no comparison.  What is being measured
// here is the loop, not the module.
#include "hls_task.h"
#include "hls_stream.h"
#include <ap_int.h>

#define DEPTH 4096          // 12-bit address; big enough that the mask is not the interesting part
#define W     16

typedef ap_uint<W> word_t;

// ---------------------------------------------------------------------------
// ING — stream-read -> BRAM-write.  The converter-facing port is the INPUT.
// ---------------------------------------------------------------------------
//
// A BRAM port has no handshake and cannot refuse, so the only thing that can stall this body is the
// input stream itself.  The question the gap measurement answers is how many cycles per firing this
// body is NOT reading -- which at an ADC boundary is exactly how many words are lost.

static void ing_body_1(word_t buf[DEPTH], hls::stream<word_t>& in) {
    static ap_uint<16> wr = 0;
    buf[wr & (DEPTH - 1)] = in.read();
    wr = wr + 1;
}

template <int N>
static void ing_body_n(word_t buf[DEPTH], hls::stream<word_t>& in) {
    static ap_uint<16> wr = 0;
    for (int i = 0; i < N; ++i) {
#pragma HLS PIPELINE II=1
        buf[wr & (DEPTH - 1)] = in.read();
        wr = wr + 1;
    }
}

static void ing_body_w(word_t buf[DEPTH], hls::stream<word_t>& in) {
    static ap_uint<16> wr = 0;
    while (1) {
#pragma HLS PIPELINE II=1
        buf[wr & (DEPTH - 1)] = in.read();
        wr = wr + 1;
    }
}

// ---------------------------------------------------------------------------
// PLY — BRAM-read -> stream-write.  The converter-facing port is the OUTPUT.
// ---------------------------------------------------------------------------
//
// No input stream at all, which is the honest reduction of the player: its two progress channels are
// polled non-blockingly and never gate it.  Whether Vitis accepts a task with no blocking input is
// itself worth knowing, so it is left as-is rather than padded with an input to be safe.
//
// The gap that matters here is on the OUTPUT: cycles per firing in which the DAC is offered nothing.

static void ply_body_1(word_t buf[DEPTH], hls::stream<word_t>& out) {
    static ap_uint<16> rd = 0;
    out.write(buf[rd & (DEPTH - 1)]);
    rd = rd + 1;
}

template <int N>
static void ply_body_n(word_t buf[DEPTH], hls::stream<word_t>& out) {
    static ap_uint<16> rd = 0;
    for (int i = 0; i < N; ++i) {
#pragma HLS PIPELINE II=1
        out.write(buf[rd & (DEPTH - 1)]);
        rd = rd + 1;
    }
}

static void ply_body_w(word_t buf[DEPTH], hls::stream<word_t>& out) {
    static ap_uint<16> rd = 0;
    while (1) {
#pragma HLS PIPELINE II=1
        out.write(buf[rd & (DEPTH - 1)]);
        rd = rd + 1;
    }
}

// ---------------------------------------------------------------------------
// The tops.  One task each, ap_ctrl_none, exactly as the real designs instantiate them.
// ---------------------------------------------------------------------------
//
// Port names are identical across every top so ONE testbench serves all four variants of a shape,
// selected by `xvlog -d KERNEL=<name>`.  A per-variant testbench would have been a place for the
// measurement to differ for a reason other than the design.

#define ING_PORTS(BODY)                                                   \
    _Pragma("HLS INTERFACE mode=bram port=buf storage_type=ram_1wnr latency=1") \
    _Pragma("HLS INTERFACE axis port=in")                                 \
    _Pragma("HLS INTERFACE ap_ctrl_none port=return")                     \
    hls_thread_local hls::task t(BODY, buf, in);

void ing_1  (word_t buf[DEPTH], hls::stream<word_t>& in) { ING_PORTS(ing_body_1) }
void ing_n8 (word_t buf[DEPTH], hls::stream<word_t>& in) { ING_PORTS(ing_body_n<8>) }
void ing_n64(word_t buf[DEPTH], hls::stream<word_t>& in) { ING_PORTS(ing_body_n<64>) }
void ing_w  (word_t buf[DEPTH], hls::stream<word_t>& in) { ING_PORTS(ing_body_w) }

#define PLY_PORTS(BODY)                                                   \
    _Pragma("HLS INTERFACE mode=bram port=buf storage_type=ram_1wnr latency=1") \
    _Pragma("HLS INTERFACE axis port=out")                                \
    _Pragma("HLS INTERFACE ap_ctrl_none port=return")                     \
    hls_thread_local hls::task t(BODY, buf, out);

void ply_1  (word_t buf[DEPTH], hls::stream<word_t>& out) { PLY_PORTS(ply_body_1) }
void ply_n8 (word_t buf[DEPTH], hls::stream<word_t>& out) { PLY_PORTS(ply_body_n<8>) }
void ply_n64(word_t buf[DEPTH], hls::stream<word_t>& out) { PLY_PORTS(ply_body_n<64>) }
void ply_w  (word_t buf[DEPTH], hls::stream<word_t>& out) { PLY_PORTS(ply_body_w) }
