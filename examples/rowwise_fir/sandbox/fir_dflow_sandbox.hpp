// fir_dflow_sandbox.hpp — Phase-1 de-risk sandbox for plans/fir-cleanup.md §1.
//
// Validates the **control-driven DATAFLOW** kernel shape that Phase 2 will
// generate: the poly control shell (ap_start-gated while(1) command loop,
// s_axilite status regs, END->return, error->halt->return) wrapping a
// hand-written `fir_dataflow(...)` whose body is a scoped `#pragma HLS DATAFLOW`
// region (load / compute / store + internal hls::streams), with the error
// leaving the region through a **depth-1 status stream**, not a shared scalar.
//
// Deliberately self-contained (only HLS headers): the built-in command
// deserialize (read_axi4_stream) and the ap_uint<mem_dwidth> serialization
// convention are Phase-2 concerns (§2). Here the command is a plain 7-word
// header read from s_in and gmem is a plain `float*` — enough to exercise the
// control protocol, the error-stream pattern, full-duplex m_axi, and the
// back-to-back seam, with a bit-exact float golden.
#pragma once

#include <hls_stream.h>
#include <ap_int.h>

namespace fir_dflow {

// ----- locked sandbox parameters -----
typedef float real_t;                  // IEEE-754 single; golden is bit-exact
static const int  T        = 8;        // FIR taps
static const int  NCOL_MAX = 1024;     // BRAM row-buffer / row-stream depth bound
static const int  MAX_ROWS = 64;       // valid-size bound (cosim m_axi depth bound)

// gmem word count: a single fixed-size shared buffer big enough for every
// sandbox scenario, so cosim's m_axi `depth=` always matches the tb array size
// (an oversized-but-matching depth is fine; a *mismatched* depth breaks cosim).
#define WF_FIR_GMEM_DEPTH 8192

// Command opcodes (plain words on s_in; Phase 2 swaps to the schema-derived
// FIRCmd.read_axi4_stream<32>).
static const unsigned FIR_OP_FIR = 0;
static const unsigned FIR_OP_END = 1;

// Error codes carried out of the DATAFLOW region on the depth-1 status stream.
static const unsigned FIR_ERR_NONE     = 0;
static const unsigned FIR_ERR_BAD_SIZE = 1;

// One FIR command header (read in the generated top, like poly's PolyCmdHdr).
struct FirCmdHdr {
    ap_uint<32> op;       // FIR_OP_FIR / FIR_OP_END
    ap_uint<32> tx_id;    // transaction id (echoed on m_out / into the halt reg)
    ap_uint<32> x_off;    // element offsets into gmem
    ap_uint<32> h_off;
    ap_uint<32> y_off;
    ap_uint<32> n_rows;
    ap_uint<32> n_cols;

    void read_stream(hls::stream<ap_uint<32>>& s) {
        op     = s.read();
        tx_id  = s.read();
        x_off  = s.read();
        h_off  = s.read();
        y_off  = s.read();
        n_rows = s.read();
        n_cols = s.read();
    }
};

// Inter-stage metadata travelling load -> compute -> store on its own stream
// (DATAFLOW processes communicate only through streams). Carries the *effective*
// row count (0 on a bad-size command, so every stage's row loop runs zero trips
// and the streams stay balanced) and the err code the store stage emits.
struct FirMeta {
    int         n_rows;   // effective (0 => error / no work)
    int         n_cols;
    int         y_off;
    ap_uint<32> tx_id;
    ap_uint<8>  err;      // FIR_ERR_*
};

}  // namespace fir_dflow

// The generated-style top: poly control shell + m_axi gmem. (Phase 2 generates
// this from FIRAccel's declared ports; here it is hand-written to de-risk.)
void fir(
    hls::stream<ap_uint<32>>& s_in,    // command words (axis)
    hls::stream<ap_uint<32>>& m_out,   // response words (axis)
    ap_uint<1>&  halted,               // s_axilite control/status
    ap_uint<8>&  error,
    ap_uint<16>& tx_id,
    fir_dflow::real_t* gmem            // m_axi shared data (X / h / Y)
);

// The hand-written hook: a scoped #pragma HLS DATAFLOW region. Returns the error
// read from the depth-1 status stream after the region completes.
namespace fir_dflow {
ap_uint<8> fir_dataflow(const FirCmdHdr& cmd, real_t* gmem,
                        hls::stream<ap_uint<32>>& m_out);
}
