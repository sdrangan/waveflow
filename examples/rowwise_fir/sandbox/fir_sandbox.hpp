// fir_sandbox.hpp — hand-written HLS load/compute/store FIR sandbox.
//
// Phase 1 of plans/dataflow_composition.md.  A self-contained, hand-authored
// HLS kernel (no Waveflow framework code) that grounds the dataflow arc and
// produces the cosim-calibrated cycle model the later `block` fidelity consumes.
// Measured results (see results/fir_sandbox_results.md):
//   * compute inner loop II = 1 (BRAM datapath, no external memory)  [confirmed]
//   * single shared m_axi bundle == split bundles: IDENTICAL latency -- a single
//     m_axi bundle is full-duplex, so a read+write pair does not contend.  This
//     CORRECTS the plan's "single-port read+write = II=2" hypothesis; the
//     #streams->II floor applies to same-direction streams (cf. VMAC), not here.
//   * steady-state throughput ~= 1.25 cycles/output (single and split alike).
//
// Real-valued floating-point FIR, *valid* edge handling, per matrix row:
//     Y[i, j] = sum_{t=0..T-1} h[t] * X[i, j + (T-1) - t],   j in [0, n_cols - T]
// Output length per row = n_cols - T + 1.
#pragma once

#include <hls_stream.h>

namespace fir_sandbox {

// ----- locked sandbox parameters (plans/dataflow_composition.md Phase 1) -----
typedef float real_t;                 // IEEE-754 single; golden is bit-exact
static const int T          = 8;      // FIR taps
static const int NCOL_MAX   = 1024;   // BRAM row-buffer depth bound
static const int N_PINGPONG = 2;      // load->compute double buffer (PIPO depth)
static const int MAX_ROWS   = 64;     // cosim m_axi depth bound (sweep uses <= 4)

}  // namespace fir_sandbox

// Top-level kernel: a #pragma HLS DATAFLOW region of three concurrent modules.
void fir_accel(
    const fir_sandbox::real_t* X,      // input matrix, row-major, n_rows x n_cols
    fir_sandbox::real_t*       Y,      // output matrix, row-major, n_rows x (n_cols-T+1)
    const fir_sandbox::real_t* h,      // FIR coefficients, length T
    int n_rows,
    int n_cols);
