#ifndef WAVEFLOW_FIR_COMPUTE_UNROLL_TASK_H
#define WAVEFLOW_FIR_COMPUTE_UNROLL_TASK_H
// fir_compute_unroll_task.h — the block FIR's stateful compute, LW OUTPUTS PER ITERATION.
//
// The vectorized twin of fir_compute_serial_task.h; FirCompute's `unroll_lane` HwParam picks which is
// emitted.  Identical arithmetic (same acc_t, same rounding, one pysim golden), different physics:
// this body consumes a whole lane per beat, so throughput scales with 1/W while the multiplier count
// stays pinned at LW*NTAP.
//
// ONE SHARED HISTORY, NOT LW COPIES.  The naive vectorization gives each lane its own delay line --
// dl[LW][NTAP], which replicates every stored sample LW times.  It is unnecessary: consecutive
// outputs read the SAME history at a one-sample offset, so a single dl[NTAP+LW-1] serves all LW lanes
// by staggering the window start:
//
//     invariant after the beat starting at i:   dl[m] == x[i + LW-1 - m]
//     lane j output:  y[i+j] = sum_k taps[k] * dl[LW-1-j + k]
//
// That costs LW-1 extra registers instead of (LW-1)*NTAP, and only the MULTIPLIERS replicate -- which
// is the part vectorization is actually buying.
//
// THE SEEDING RULE (this kernel's recurring bug, twice over).  The beat SHIFTS BEFORE it accumulates,
// so what gets seeded is the state at the TOP of the first beat -- pre-shift.  The shift moves
// dl[j] -> dl[LW+j], so seeding `dl[j] = carry[NTAP-2-j]` puts the history where the invariant above
// needs it.  Seeding dl[LW+j] directly (writing down the post-shift invariant) reads correct and is
// wrong: it double-shifts the history and drops the newest carry sample.  Verified index-for-index
// against the golden, including unaligned block lengths, before this was ever synthesized.
//
// THE TAIL.  When n is not a multiple of LW the final beat carries LW-nb_last padding samples, which
// land in the LOW dl entries.  The valid history is therefore offset by `off = LW - nb_last`, and the
// carry is taken from dl[off + NTAP-2-j].  off == 0 in the aligned case, so it collapses to the
// serial body's spelling.
//
// Deserialization is the generated fir_au::read/write_framed_stream_lane, never a hand-rolled
// .range() -- see guide/vectorization/hls/arrayutils.md.  Its pysim twin is FirCompute.run_iter.
#include "hls_stream.h"
#include <ap_int.h>
#include <ap_fixed.h>
#include "streamutils_hls.h"
#include "fir_op.h"
#include "fir_desc.h"
#include "mem_w_cmd.h"
#include "fir_types.h"

template <int MEM_DW>
static void fir_compute_unroll_task(hls::stream<streamutils::framed_word<MEM_DW> >& s_in,
                                    hls::stream<streamutils::framed_word<MEM_DW> >& cmd_out) {
    typedef fir_au::value_type samp_t;
    typedef fir_types::acc_t acc_t;
    const int NTAP = fir_types::NTAP;
    const int LW = fir_au::lane_capacity<MEM_DW>();

// Cross-firing state, emitted from FirCompute.add_state (see the serial body's note on the site).
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

        samp_t dl[NTAP + LW - 1];                 // ONE history, LW staggered windows over it
#pragma HLS ARRAY_PARTITION variable=dl complete dim=1
    SEED: for (int j = 0; j < NTAP - 1; ++j) {    // PRE-shift: the first beat moves dl[j] -> dl[LW+j]
#pragma HLS UNROLL
            dl[j] = (d.zero_state != 0) ? (samp_t)0 : carry[NTAP - 2 - j];
        }

    FIRV: for (int i = 0; i < n; i += LW) {
#pragma HLS PIPELINE II=1
#pragma HLS LOOP_TRIPCOUNT max=2048
            fir_au::read_framed_stream_lane<MEM_DW>(s_in, ilane, LW, tl);
        SH: for (int m = NTAP + LW - 2; m >= LW; --m) {
#pragma HLS UNROLL
                dl[m] = dl[m - LW];
            }
        INS: for (int j = 0; j < LW; ++j) {
#pragma HLS UNROLL
                dl[LW - 1 - j] = ilane[j];
            }
        LANE: for (int j = 0; j < LW; ++j) {      // LW independent windows -> LW*NTAP multipliers
#pragma HLS UNROLL
                acc_t acc = 0;
            MAC: for (int m = 0; m < NTAP; ++m) {
#pragma HLS UNROLL
                    acc += (acc_t)(taps[m] * dl[LW - 1 - j + m]);
                }
                olane[j] = (samp_t)acc;
            }
            const int nb = (n - i < LW) ? (n - i) : LW;
            fir_au::write_framed_stream_lane<MEM_DW>(olane, cmd_out, (i + LW >= n), nb);
        }

        // The tail offset: padding in the final beat sits in the low dl entries, so the valid
        // history starts `off` slots up.  Zero when n is a multiple of LW.
        const int off = LW - (n - (nw - 1) * LW);
    SAVE: for (int j = 0; j < NTAP - 1; ++j) {
            carry[j] = dl[off + NTAP - 2 - j];
        }
    }
}

#endif  // WAVEFLOW_FIR_COMPUTE_UNROLL_TASK_H
