// fir_freerun_sandbox.hpp — free-running streaming FIR sandbox
// (plans/fir_freerun_sandbox.md).
//
// The free-running alternative to the control-driven sandbox: an ap_ctrl_hs top
// with a single #pragma HLS DATAFLOW region of three PERSISTENT while(!done)
// processes — load / compute / store — wired by plain hls::stream FIFOs.  Jobs
// PIPELINE across the network (load(N+1) overlaps store(N) on the gmem bundle),
// unlike the control-driven `int err = fir_dataflow(cmd); if(err) return;`
// per-job barrier that serialized them (~1467 cyc/cmd, dflow_notes.md Finding 4).
//
// Compute is a streaming SHIFT-REGISTER FIR: a T-deep fully-partitioned tapped
// delay line + unrolled MAC at II=1, reading X sample-by-sample.  No row buffer,
// no per-row #pragma HLS DATAFLOW, no fir_accel_core.  The window flushes at each
// row boundary (the FIR is per row; row i's window must not pull row i-1).
//
// §2b boundary serialization: gmem is ap_uint<MEM_DW>* (NOT float*); every memory
// access goes through read_array_slice<W> / write_array_slice<W> (ap_uint<->float
// deserialize).  The internal FIFOs carry float — already deserialized.
#pragma once

#include <hls_stream.h>
#include <ap_int.h>
#include <cstdint>

namespace fir_freerun {

typedef float real_t;                 // IEEE-754 single; golden is bit-exact
static const int T        = 8;        // FIR taps (tapped-delay-line depth)
static const int NCOL_MAX = 1024;     // valid-size bound
static const int MAX_ROWS = 64;       // valid-size bound
static const int MEM_DW   = 32;       // m_axi data width (mem_dwidth); 1 float/word
static const int CHUNK    = 256;      // burst staging chunk (one X/Y burst per test job)

// m_axi region size; the cosim memory model is sized from this, so it must equal
// the testbench gmem array length.
#define WF_FIR_FR_GMEM_DEPTH 8192

// Command opcodes (in-band on s_in) and per-job response status codes (on m_out).
static const unsigned OP_FIR      = 0;
static const unsigned OP_END      = 1;
static const unsigned ST_OK       = 0;
static const unsigned ST_BAD_SIZE = 1;

typedef ap_uint<32> word_t;           // s_in / m_out word

// Per-job control/metadata flowing load -> compute -> store on its own ctrl FIFO
// (DATAFLOW processes communicate only through streams).  `status` carries the
// per-job error to `store` (which emits it in the response) — no global halt.
struct Cmd {
    ap_uint<2>  op;
    ap_uint<16> tx_id;
    int x_off, h_off, y_off, n_rows, n_cols;
    ap_uint<8>  status;
};

// --- §2b boundary serialization: bit-cast ap_uint<32> <-> float (1 float/word) ---
static inline float bits_to_float(ap_uint<32> u) {
    union { uint32_t i; float f; } c;
    c.i = (uint32_t)u;
    return c.f;
}
static inline ap_uint<32> float_to_bits(float f) {
    union { uint32_t i; float f; } c;
    c.f = f;
    return ap_uint<32>(c.i);
}

// read_array_slice<W> / write_array_slice<W>: element-coordinate (de)serialize at
// the m_axi boundary (W = MEM_DW = 32 => one float per word).  Sequential gmem
// access so Vitis infers an AXI burst; the only place ap_uint<->float happens.
template<int W>
inline void read_array_slice(const ap_uint<W>* words, int i0, int i1, float* out) {
    int o = 0;
RAS: for (int i = i0; i < i1; ++i) {
#pragma HLS PIPELINE II=1
        out[o++] = bits_to_float(words[i]);
    }
}
template<int W>
inline void write_array_slice(const float* in, ap_uint<W>* words, int i0, int i1) {
    int o = 0;
WAS: for (int i = i0; i < i1; ++i) {
#pragma HLS PIPELINE II=1
        words[i] = float_to_bits(in[o++]);
    }
}

}  // namespace fir_freerun

// Free-running streaming FIR top: ap_ctrl_hs + #pragma HLS DATAFLOW.
void fir(hls::stream<fir_freerun::word_t>& s_in,
         hls::stream<fir_freerun::word_t>& m_out,
         ap_uint<fir_freerun::MEM_DW>* gmem);
