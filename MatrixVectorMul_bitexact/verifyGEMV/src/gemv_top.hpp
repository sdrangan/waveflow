// gemv_top.hpp -- the designs under test: AMD Vitis BLAS L1 gemv.
//
// Three tops, because three separate questions need synthesized RTL to answer and native
// C-simulation cannot reach any of them:
//
//   gemv_f32_top    the 5-arg float overload -- does the dot_tree reduction survive to RTL?
//   gemv_ab_top     the 8-arg alpha/beta overload -- does HLS FUSE axpy's `alpha*x + y`?
//   gemv_fixed_top  the ap_fixed path -- does the dotHelper.hpp:98 defect survive to RTL?
//
// See ARCHITECTURE.md for what each one is and which number formats it uses.
#ifndef GEMV_TOP_HPP_
#define GEMV_TOP_HPP_

#include <ap_fixed.h>
#include <ap_int.h>
#include <hls_stream.h>
#include "xf_blas.hpp"

// Sizes are compile-time so the RTL is bounded and the interface is streams only.  N=64 with
// P=4 gives 16 beats and 4 chunks -- the smallest size at which the library's reduction and a
// plain binary tree are DIFFERENT reductions, so the check is not vacuous.  (Below 4 chunks they
// coincide; see PLAN.md, S5.)
#define GEMV_M 4
#define GEMV_N 64
#define GEMV_LOGP 2
#define GEMV_P (1 << GEMV_LOGP)

// The ap_fixed DUT uses its own, smaller size: the defect it demonstrates is per-row, so a
// larger case would only make cosim slower.
#define GEMVF_M 3
#define GEMVF_N 32
#define GEMVF_W 16
#define GEMVF_I 8

typedef float T_f32;
typedef ap_fixed<GEMVF_W, GEMVF_I> T_fixed;

// The tops take ARRAYS and do the stream plumbing internally, which is the shape the library's
// own testbench uses (L1/tests/hw/gemv/uut_top.cpp).  That is not cosmetic: with hls::stream as
// top-level arguments, csim and csynth both pass but co-simulation aborts with
//
//     ERROR [HLS SIM]: an hls::stream is read while empty
//
// while instrumenting the C testbench.  Arrays become ap_memory ports and cosim is happy.
void gemv_f32_top(T_f32 p_A[GEMV_M * GEMV_N], T_f32 p_x[GEMV_N], T_f32 p_y[GEMV_M]);

void gemv_ab_top(T_f32 p_alpha, T_f32 p_beta,
                 T_f32 p_A[GEMV_M * GEMV_N], T_f32 p_x[GEMV_N],
                 T_f32 p_y[GEMV_M], T_f32 p_yr[GEMV_M]);

void gemv_fixed_top(T_fixed p_A[GEMVF_M * GEMVF_N], T_fixed p_x[GEMVF_N], T_fixed p_y[GEMVF_M]);

#endif
