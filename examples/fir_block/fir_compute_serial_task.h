#ifndef WAVEFLOW_FIR_COMPUTE_SERIAL_TASK_H
#define WAVEFLOW_FIR_COMPUTE_SERIAL_TASK_H
// fir_compute_serial_task.h — the block FIR's stateful compute, ONE OUTPUT PER ITERATION.
//
// One of two realizations of the same arithmetic (see fir_compute_unroll_task.h); FirCompute's
// `unroll_lane` HwParam picks which one is emitted.  Both compute bit-identical results -- same
// acc_t, same rounding, one pysim golden -- so the parameter is a pure QoR knob:
//
//   this body   : 1 sample/cycle regardless of W; the multiplier count FALLS with precision
//                 (T=32, MEM_DW=32 -> 64 DSP at W=24, 32 at W=16, 17 at W=8, where Vitis packs two
//                 8x8 multiplies per DSP48E1).  The "smaller W, smaller design" story.
//   unroll body : LW samples/cycle; multiplier count pinned at LW*T.  The "smaller W, faster" story.
//
// The lane index k is carried across iterations, so the stream is touched only every LW-th one -- a
// conditional dequeue AND a conditional enqueue inside an II=1 body.  Measured: it DOES schedule at
// II=1; the conditionals cost 3 cycles of iteration latency (12 vs 9), not initiation interval.
//
// Deserialization is the generated fir_au::read_framed_stream_lane, never a hand-rolled .range() --
// see guide/vectorization/hls/arrayutils.md.  That routine and the Python twin's DataArray.serialize
// are one packing contract, which is what keeps the golden and the RTL bit-exact at any LW.
//
// Both opcodes, and the storage, work exactly as in the unroll body; see that header's comment for
// the LOAD_TAPS / len=0 completion argument.  Its pysim twin is FirCompute.run_iter.
#include "hls_stream.h"
#include <ap_int.h>
#include <ap_fixed.h>
#include "streamutils_hls.h"
#include "fir_op.h"
#include "fir_desc.h"
#include "mem_w_cmd.h"
#include "fir_types.h"

template <int MEM_DW>
static void fir_compute_serial_task(hls::stream<streamutils::framed_word<MEM_DW> >& s_in,
                                    hls::stream<streamutils::framed_word<MEM_DW> >& cmd_out) {
    typedef fir_au::value_type samp_t;
    typedef fir_types::acc_t acc_t;
    const int NTAP = fir_types::NTAP;
    const int LW = fir_au::lane_capacity<MEM_DW>();

// Cross-firing state, emitted from FirCompute.add_state -- included INSIDE the body because that is
// the site declared state lands at for a free-running hls::task (a task has no "before the loop", so
// this declaration IS the initialization site, and `static` here is what survives re-firing).
#include "fir_compute_state.inc"

    FirDesc d;
    streamutils::tlast_status tl;
    d.read_framed_stream<MEM_DW>(s_in, tl);
    const int n = (int)d.n;                       // SAMPLES; the stream carries ceil(n/LW) words
    const int nw = (n + LW - 1) / LW;
    samp_t ilane[LW], olane[LW];
#pragma HLS ARRAY_PARTITION variable=ilane complete dim=1
#pragma HLS ARRAY_PARTITION variable=olane complete dim=1

    if (d.op == FirOp::LOAD_TAPS) {
    LOAD: for (int t0 = 0; t0 < n; t0 += LW) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT max=NTAP
            fir_au::read_framed_stream_lane<MEM_DW>(s_in, ilane, LW, tl);
        PUT: for (int j = 0; j < LW; ++j) {
#pragma HLS UNROLL
                if (t0 + j < n) taps[t0 + j] = ilane[j];
            }
        }
        // len=0: no data follows and no AXI write happens, but fwd_bursts=1 still carries the
        // descriptor to s_done, so the no-output opcode completes like any other job.
        MemWCmd memw;
        memw.addr = d.dst_off;
        memw.len = 0;
        memw.fwd_bursts = 1;
        memw.write_framed_stream<MEM_DW>(cmd_out);
        d.write_framed_stream<MEM_DW>(cmd_out);
    } else {
        MemWCmd memw;
        memw.addr = d.dst_off;
        memw.len = nw;
        memw.fwd_bursts = 1;
        memw.write_framed_stream<MEM_DW>(cmd_out);
        d.write_framed_stream<MEM_DW>(cmd_out);

        // dline[k] == x[i-k] at MAC time.  Each iteration SHIFTS BEFORE it accumulates, so what is
        // seeded is the state at the TOP of iteration 0, where dline[j] == x[-1-j] == carry[NTAP-2-j].
        // Seeding the MAC-time invariant instead silently drops the newest carry sample.
        samp_t dline[NTAP];
#pragma HLS ARRAY_PARTITION variable=dline complete dim=1
    SEED: for (int j = 0; j < NTAP - 1; ++j) {
#pragma HLS UNROLL
            dline[j] = (d.zero_state != 0) ? (samp_t)0 : carry[NTAP - 2 - j];
        }
        dline[NTAP - 1] = (samp_t)0;              // shifted out before any MAC reads it

        int k = 0;                                 // lane index, carried across iterations
    FIR: for (int i = 0; i < n; ++i) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT max=4096
            if (k == 0) fir_au::read_framed_stream_lane<MEM_DW>(s_in, ilane, LW, tl);
            samp_t x = ilane[k];
        SHIFT: for (int m = NTAP - 1; m > 0; --m) {
#pragma HLS UNROLL
                dline[m] = dline[m - 1];
            }
            dline[0] = x;
            acc_t acc = 0;                          // exact: acc_t carries the product's frac bits
        MAC: for (int m = 0; m < NTAP; ++m) {
#pragma HLS UNROLL
                acc += (acc_t)(taps[m] * dline[m]);
            }
            olane[k] = (samp_t)acc;                 // the declared quantize (AP_TRN / AP_WRAP)
            ++k;
            if (k == LW || i + 1 == n) {            // flush a full lane, or the final partial one
                fir_au::write_framed_stream_lane<MEM_DW>(olane, cmd_out, (i + 1 == n), k);
                k = 0;
            }
        }

    SAVE: for (int j = 0; j < NTAP - 1; ++j) {
#pragma HLS UNROLL
            carry[j] = dline[NTAP - 2 - j];         // the last NTAP-1 samples, oldest first
        }
    }
}

#endif  // WAVEFLOW_FIR_COMPUTE_SERIAL_TASK_H
