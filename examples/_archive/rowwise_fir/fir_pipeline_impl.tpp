// fir_pipeline_impl.tpp — the hand-written free-running streaming FIR hook.
//
// Included from gen/fir.hpp (which brings streamutils + the generated top decl into scope).
// This is the validated sandbox kernel (sandbox/fir_freerun_sandbox.cpp) adapted to the
// generated hook signature `fir_impl::pipeline(s_in, m_out, mem)`, with the sandbox's de-risk
// command-handling shortcuts FIXED:
//   * the command is read off s_in via a DATAFLOW-safe fixed-beat read (read exactly
//     FIRCmd::nwords beats unconditionally, then FIRCmd::read_array deserialize) — NOT
//     read_axi4_stream, whose ~23 TLAST early-return branches break DATAFLOW liveness in
//     cosim — and not manual per-field s_in.read() casts;
//   * the internal load->compute->store ctrl channel is a fixed-width 32-bit serialized stream
//     of the reduced FirMeta schema (n_rows/n_cols/y_off/tx_id/status) via the built-in
//     write_stream/read_stream serializers, not a wide hls::stream<Cmd> struct FIFO.
//
// Structure: three persistent `while(!done)` DATAFLOW processes over plain hls::stream FIFOs,
// shift-register FIR (II=1, per-row window flush), END-sentinel drain, per-job status on m_out.
// §2b: gmem is ap_uint<MEM_DW>* accessed ONLY via read_array_slice/write_array_slice (the
// internal data FIFOs carry already-deserialized float).
#pragma once

#include "include/f_i_r_op.h"
#include "include/f_i_r_status.h"
#include "include/f_i_r_cmd.h"
#include "include/f_i_r_resp.h"
#include "include/fir_meta.h"
#include "include/float32_array_utils.h"

namespace fir_impl {

static const int FIR_T        = 8;       // FIR taps (tapped-delay-line depth)
static const int FIR_NCOL_MAX = 1024;    // valid-size bound
static const int FIR_MAX_ROWS = 64;      // valid-size bound

// LW==1 only: the compute uses a scalar shift register sr[T] and emits one output/cycle, and the
// load/store lane loops move one float/cycle.  A wider m_axi word (word_bw>32 -> LW>1) would need an
// s[LW][T] window + an LW*T MAC array to retire LW outputs/cycle — not yet supported.  Fail loudly.
static_assert(float32_array_utils::lane_capacity<32>() == 1,
              "fir_pipeline_impl supports LW==1 only; LW>1 (vectorized bus) needs an s[LW][T] window.");

// load: read each FIRCmd off s_in (built-in deserialize); validate; forward a FirMeta on the
// 32-bit ctrl stream; for a good job read h (T taps) + X (CHUNK bursts) from mem via
// read_array_slice and stream them as float.  A bad-size job streams NO data (status carries it).
template<int AXIS_DW>
static void fir_load(hls::stream<streamutils::axi4s_word<AXIS_DW>>& s_in,
                     const ap_uint<32>* mem,
                     hls::stream<ap_uint<32>>& ld_ctrl,
                     hls::stream<float>& ld_data) {
    bool done = false;
LOAD_CMDS: while (!done) {
        // FixedBeat command read: read exactly N beats unconditionally (no TLAST early-return
        // -> regular channel access -> DATAFLOW-safe), then schema-deserialize from the word
        // buffer.  The host always sends a full N-word FIRCmd for every command (incl. END),
        // so a fixed N-beat read is correct for all commands; Stage A skips TLAST framing
        // validation (bit-exactness doesn't depend on it).
        static const int CMD_NW = FIRCmd::nwords<AXIS_DW>();   // = 7 at AXIS_DW=32
        ap_uint<AXIS_DW> cw[CMD_NW];
        for (int i = 0; i < CMD_NW; ++i) {
            auto axis_word = s_in.read();      // unconditional, fixed trip count
            cw[i] = axis_word.data;
        }
        FIRCmd cmd;
        cmd.read_array<AXIS_DW>(cw);           // generated unpack, no TLAST branching

        FirMeta m;
        m.n_rows = (ap_uint<16>)cmd.n_rows;
        m.n_cols = (ap_uint<16>)cmd.n_cols;
        m.y_off  = (ap_uint<32>)cmd.y_off;
        m.tx_id  = (ap_uint<16>)cmd.tx_id;
        if (cmd.op == FIROp::end) {
            m.status = FIRStatus::end;
            m.write_stream<32>(ld_ctrl);             // drain marker on the 32-bit ctrl channel
            done = true; break;
        }
        const int nr = (int)cmd.n_rows, nc = (int)cmd.n_cols;
        const bool ok = (nc >= FIR_T && nc <= FIR_NCOL_MAX && nr >= 1 && nr <= FIR_MAX_ROWS);
        m.status = ok ? FIRStatus::ok : FIRStatus::bad_size;
        m.write_stream<32>(ld_ctrl);
        if (!ok) continue;                           // bad job: no data streamed

        // h (T taps) is genuinely resident — read once, cached in compute's tap registers — so a
        // slice into a small buffer is the right pattern for it.
        float hb[FIR_T];
        float32_array_utils::read_array_slice<32>(mem, (int)cmd.h_off, (int)cmd.h_off + FIR_T, hb);
        for (int t = 0; t < FIR_T; ++t) ld_data.write(hb[t]);

        // X is a STREAM: move gmem -> ld_data DIRECTLY, one II=1 pass, no intermediate buffer (the
        // lane-loop shape for LW==1; docs/guide/vectorization/hls/raw.md).  Staging through a
        // read_array_slice buffer (gmem -> cb -> FIFO, the "resident" path) serializes the m_axi and
        // forfeits the full-duplex load||store overlap — see [[project-fir-slice-vs-laneloop-rootcause]].
        // (NB the framework read_array_lane helper with a running pointer dangles the m_axi first read
        // in this free-running DATAFLOW context — RTL reads x[0] as 0; direct mem[i] indexing is
        // bit-exact.  A framework gotcha to fix; see [[reference-arrayutils-slice-codegen-gotchas]].)
        const int total = nr * nc;
    LOAD_X: for (int i = 0; i < total; ++i) {
#pragma HLS PIPELINE II=1
            ld_data.write(streamutils::uint_to_float((uint32_t)mem[(int)cmd.x_off + i]));
        }
    }
}

// compute: streaming shift-register FIR.  Forward the FirMeta to store, then for a good job
// cache the T taps and emit n_cols-T+1 outputs/row at II=1 (per-row window flush; tap order
// t=0..T-1 with sr[t]=x[s-t] reproduces fir_golden -> bit-exact).
static void fir_compute(hls::stream<ap_uint<32>>& ld_ctrl, hls::stream<float>& ld_data,
                        hls::stream<ap_uint<32>>& cp_ctrl, hls::stream<float>& cp_data) {
    bool done = false;
COMPUTE_CMDS: while (!done) {
        FirMeta m;
        m.read_stream<32>(ld_ctrl);
        m.write_stream<32>(cp_ctrl);                 // forward (incl. end / bad_size)
        if (m.status == FIRStatus::end) { done = true; break; }
        if (m.status != FIRStatus::ok) continue;     // bad job: no data

        float h[FIR_T];
#pragma HLS ARRAY_PARTITION variable=h complete dim=1
        for (int t = 0; t < FIR_T; ++t) h[t] = ld_data.read();

        const int nr = (int)m.n_rows, nc = (int)m.n_cols;
    ROWS: for (int r = 0; r < nr; ++r) {
            float sr[FIR_T];                          // tapped delay line: sr[t] = x[s-t]
#pragma HLS ARRAY_PARTITION variable=sr complete dim=1
            for (int k = 0; k < FIR_T; ++k) sr[k] = 0.0f;   // per-row window flush
        COLS: for (int s = 0; s < nc; ++s) {
#pragma HLS PIPELINE II=1
                const float x = ld_data.read();
                for (int k = FIR_T - 1; k >= 1; --k) {
#pragma HLS UNROLL
                    sr[k] = sr[k - 1];
                }
                sr[0] = x;
                if (s >= FIR_T - 1) {
                    float acc = 0.0f;
                    for (int t = 0; t < FIR_T; ++t) {
#pragma HLS UNROLL
                        acc += h[t] * sr[t];          // left-to-right -> bit-exact golden
                    }
                    cp_data.write(acc);
                }
            }
        }
    }
}

// store: drain Y from cp_data, write it to mem via write_array_slice (CHUNK bursts), and emit
// one FIRResp{tx_id, status} per job on m_out.  A bad-size job writes no Y and flags status=1 —
// the pipeline keeps flowing (no global halt).
template<int AXIS_DW>
static void fir_store(hls::stream<ap_uint<32>>& cp_ctrl, hls::stream<float>& cp_data,
                      ap_uint<32>* mem,
                      hls::stream<streamutils::axi4s_word<AXIS_DW>>& m_out) {
    bool done = false;
STORE_CMDS: while (!done) {
        FirMeta m;
        m.read_stream<32>(cp_ctrl);
        if (m.status == FIRStatus::end) { done = true; break; }   // END carries no response

        if (m.status == FIRStatus::ok) {
            const int nc = (int)m.n_cols, nr = (int)m.n_rows;
            const int out_len = nc - FIR_T + 1;
            const int total = nr * out_len;
            // Y is a STREAM: cp_data -> gmem DIRECTLY (one II=1 pass, no cb buffer) — the symmetric
            // fix to the load (see [[project-fir-slice-vs-laneloop-rootcause]]).
        STORE_Y: for (int i = 0; i < total; ++i) {
#pragma HLS PIPELINE II=1
                mem[(int)m.y_off + i] = streamutils::float_to_uint(cp_data.read());
            }
        }
        FIRResp resp;
        resp.tx_id = (ap_uint<32>)m.tx_id;
        resp.status = (ap_uint<32>)((m.status == FIRStatus::bad_size) ? 1u : 0u);
        resp.write_axi4_stream<AXIS_DW>(m_out, true);
    }
}

// pipeline: the hook the generated ap_ctrl_hs top calls.  One #pragma HLS DATAFLOW region of the
// three persistent processes over plain hls::stream FIFOs (job-sized data FIFOs enable the
// inter-job overlap: load(N+1) reads while store(N) writes the same gmem bundle).
template<int mem_dwidth>
void pipeline(hls::stream<streamutils::axi4s_word<mem_dwidth>>& s_in,
              hls::stream<streamutils::axi4s_word<mem_dwidth>>& m_out,
              ap_uint<32>* mem) {
#pragma HLS DATAFLOW
    hls::stream<ap_uint<32>> ld_ctrl;
#pragma HLS STREAM variable=ld_ctrl depth=8
    hls::stream<float> ld_data;
#pragma HLS STREAM variable=ld_data depth=1024
    hls::stream<ap_uint<32>> cp_ctrl;
#pragma HLS STREAM variable=cp_ctrl depth=8
    hls::stream<float> cp_data;
#pragma HLS STREAM variable=cp_data depth=1024

    fir_load<mem_dwidth>(s_in, mem, ld_ctrl, ld_data);
    fir_compute(ld_ctrl, ld_data, cp_ctrl, cp_data);
    fir_store<mem_dwidth>(cp_ctrl, cp_data, mem, m_out);
}

}  // namespace fir_impl
