// gemv_top.hpp -- the designs under test: AMD Vitis BLAS L1 gemv.
//
// Nine tops.  Each exists because some claim about the kernel could not be checked without
// synthesized RTL, and the set is chosen to close every "not verified" item in ../VERIFY.md:
//
//   gemv_f32_top     the 5-arg float overload            -- does dot_tree survive to RTL?
//   gemv_f32_wide_top  float at a DIFFERENT stream width -- is P=4 special, or does it generalise?
//   gemv_f32_pad_top   float where the beat count needs PADDING -- a path no other DUT reaches
//   gemv_ab_top      the 8-arg alpha/beta overload       -- does HLS FUSE axpy's alpha*x + y?
//   gemv_f64_top     double                              -- AdderDelay 8 rather than 4
//   gemv_i32_top     int32_t                             -- the dot_dsp path, never synthesized before
//   gemv_u32_top     ap_uint<32>                         -- same bits as int32, different meaning
//   gemv_fixed_top   ap_fixed<16,8>                      -- is the dotHelper.hpp:98 defect real silicon?
//   gemv_fix24_top   ap_fixed<24,12>                     -- WideType packs by sizeof(T)*8, which is
//                                                          32 bits in csim and 24 under synthesis
//
// See ARCHITECTURE.md for what each one is and which number formats it uses.
#ifndef GEMV_TOP_HPP_
#define GEMV_TOP_HPP_

#include <ap_fixed.h>
#include <ap_int.h>
#include <hls_stream.h>
#include "xf_blas.hpp"

// Generated from data/user_input.txt by make_user_dut.py -- the "bring your own input" DUT.
// A default is committed so the package builds out of the box; re-run the script to replace it.
#include "gemv_user_cfg.hpp"

// Sizes are compile-time so the RTL is bounded and the interface is arrays only.  The testbenches
// refuse to run if an input file disagrees with the DUT it is driving.
//
// Beat and chunk arithmetic decides whether a size is worth synthesizing.  With B = N/P beats and
// Delays beats per chunk (4 for float, 8 for double), a left-fold and a balanced tree coincide
// below 4 chunks -- so a DUT with fewer cannot tell a correct reduction from a plain tree.
//
//   f32       M=4 N=64  P=4   -> 16 beats, 4 chunks, no padding   (discriminating)
//   f32_wide  M=3 N=128 P=8   -> 16 beats, 4 chunks, no padding   (discriminating, wider stream)
//   f32_pad   M=2 N=208 P=16  -> 13 beats, PADDED to 16, 4 chunks (padding AND discriminating)
//   f64       M=2 N=64  P=2   -> 32 beats, 4 chunks of 8          (discriminating)
#define GEMV_M 4
#define GEMV_N 64
#define GEMV_LOGP 2
#define GEMV_P (1 << GEMV_LOGP)

#define GEMVW_M 3
#define GEMVW_N 128
#define GEMVW_LOGP 3
#define GEMVW_P (1 << GEMVW_LOGP)

#define GEMVPD_M 2
#define GEMVPD_N 208
#define GEMVPD_LOGP 4
#define GEMVPD_P (1 << GEMVPD_LOGP)

#define GEMVD_M 2
#define GEMVD_N 64
#define GEMVD_LOGP 1
#define GEMVD_P (1 << GEMVD_LOGP)

// The non-float DUTs share one size: dot_dsp accumulates in index order at any stream width, so
// sweeping the width here would add cosim time and no information.
#define GEMVI_M 3
#define GEMVI_N 32
#define GEMVI_LOGP 2
#define GEMVI_P (1 << GEMVI_LOGP)

#define GEMVF_M 3
#define GEMVF_N 32
#define GEMVF_W 16
#define GEMVF_I 8

// ap_fixed<24,12>: sizeof is 4 bytes, so WideType gives it a 32-bit slot in C-simulation while
// the value is 24 bits wide.  Under __SYNTHESIS__ the container is exact.  If that difference
// leaked into the data path, csim and cosim would disagree here and nowhere else.
#define GEMVF24_M 3
#define GEMVF24_N 32
#define GEMVF24_W 24
#define GEMVF24_I 12

typedef float T_f32;
typedef double T_f64;
typedef int32_t T_i32;
typedef ap_uint<32> T_u32;
typedef ap_fixed<GEMVF_W, GEMVF_I> T_fixed;
typedef ap_fixed<GEMVF24_W, GEMVF24_I> T_fix24;

// The tops take ARRAYS and do the stream plumbing internally, which is the shape the library's
// own testbench uses (L1/tests/hw/gemv/uut_top.cpp).  That is not cosmetic: with hls::stream as
// top-level arguments, csim and csynth both pass but co-simulation aborts with
//
//     ERROR [HLS SIM]: an hls::stream is read while empty
//
// while instrumenting the C testbench.  Arrays become ap_memory ports and cosim is happy.
void gemv_f32_top(T_f32 p_A[GEMV_M * GEMV_N], T_f32 p_x[GEMV_N], T_f32 p_y[GEMV_M]);
void gemv_f32_wide_top(T_f32 p_A[GEMVW_M * GEMVW_N], T_f32 p_x[GEMVW_N], T_f32 p_y[GEMVW_M]);
void gemv_f32_pad_top(T_f32 p_A[GEMVPD_M * GEMVPD_N], T_f32 p_x[GEMVPD_N], T_f32 p_y[GEMVPD_M]);

void gemv_ab_top(T_f32 p_alpha, T_f32 p_beta,
                 T_f32 p_A[GEMV_M * GEMV_N], T_f32 p_x[GEMV_N],
                 T_f32 p_y[GEMV_M], T_f32 p_yr[GEMV_M]);

void gemv_f64_top(T_f64 p_A[GEMVD_M * GEMVD_N], T_f64 p_x[GEMVD_N], T_f64 p_y[GEMVD_M]);
void gemv_i32_top(T_i32 p_A[GEMVI_M * GEMVI_N], T_i32 p_x[GEMVI_N], T_i32 p_y[GEMVI_M]);
void gemv_u32_top(T_u32 p_A[GEMVI_M * GEMVI_N], T_u32 p_x[GEMVI_N], T_u32 p_y[GEMVI_M]);
void gemv_fixed_top(T_fixed p_A[GEMVF_M * GEMVF_N], T_fixed p_x[GEMVF_N], T_fixed p_y[GEMVF_M]);
void gemv_fix24_top(T_fix24 p_A[GEMVF24_M * GEMVF24_N], T_fix24 p_x[GEMVF24_N],
                    T_fix24 p_y[GEMVF24_M]);

void gemv_user_top(T_user p_A[WF_USER_M * WF_USER_N], T_user p_x[WF_USER_N],
                   T_user p_y[WF_USER_M]);

#endif
