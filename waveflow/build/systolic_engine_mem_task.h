// systolic_engine_mem_task.h -- the systolic engine behind memory: the sequencer and the writer.
//
// systolic_engine_seq_task: one job per firing (EngineJobDesc: C = q(A B) with M x K x N in memory).
// Validates it, then runs the output-stationary loop: per C tile (i, j) of MMAX x NMAX, an EngineFeed to
// the feeder (where A(i,0) and B(0,j) are, and the step between B panels) and the tile's word to the
// writer (its C address, whether it is the job's last).  A refused job: an empty feed (one feeder firing
// for the writer's one done write -- the m_axi pointer FIFOs of one top couple their firing counts).
// Block-major memory: A(i,p) at a_base + (i P + p) WA, B(p,j) at b_base + (p NT + j) WB,
// C(i,j) at c_base + (i NT + j) WC.
//
// systolic_engine_feed_task: one tile per firing, the read port's owner.  Writes the tile's EngineCmd,
// then A(i,0) B(0,j) A(i,1) B(1,j) ...: every panel's read requested ahead (hls::burst_maxi), then the
// words in one loop, a word a cycle (last on each panel's final word).
//
// systolic_engine_writer_task: one tile (or refused job) per firing.  Takes the engine's Resp and C
// and its Status, and writes MemWCmd(C tile, fwd = last) | [EngineDone] | C.
// Python twins: waveflow.linalg.systolic_engine_mem.EngineSeq / EngineFeeder / EngineWriter.
#ifndef WF_SYSTOLIC_ENGINE_MEM_TASK_H
#define WF_SYSTOLIC_ENGINE_MEM_TASK_H
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "wf_linalg_msg.h"
#include "mem_w_cmd.h"
#include "wf_engine_cmd.h"
#include "wf_engine_resp.h"
#include "wf_engine_status.h"
#include "wf_engine_job_desc.h"
#include "wf_engine_done.h"
#include "wf_engine_tile.h"
#include "wf_engine_feed.h"
#include "hls_burst_maxi.h"

template <int WBW, int MMAX, int KMAX, int NMAX, int PMAX, int WA, int WB, int WC, typename IN_T>
static void systolic_engine_seq_task(hls::stream<IN_T>& s_job, hls::stream<ap_uint<64> >& feed_out,
                                     hls::stream<ap_uint<64> >& tile_out) {
    EngineJobDesc d;
    streamutils::read_msg<WBW>(d, s_job);
    const int op = d.op, M = d.M, K = d.K, N = d.N;
    int st = wf_linalg::OK;
    if (op != 1 && op != 2) {
        st = wf_linalg::BAD_OP;
    } else if (M < MMAX || K < KMAX || N < NMAX || M % MMAX != 0 || K % KMAX != 0 ||
               N % NMAX != 0 || K / KMAX > PMAX) {
        st = wf_linalg::BAD_DIMS;
    }
    EngineTile t;
    EngineFeed f;
    t.tx_id = d.tx_id;
    f.tx_id = d.tx_id;
    f.op = op;
    if (st != wf_linalg::OK) {
        t.c_addr = 0;
        t.status = st;
        t.last = 1;
        t.empties = 0;
        t.template write_stream<64>(tile_out);
        f.a_addr = 0;
        f.b_addr = 0;
        f.b_step = 0;
        f.npanels = 0;
        f.refused = 1;
        f.template write_stream<64>(feed_out);
        return;
    }
    const int MT = M / MMAX, NT = N / NMAX, P = K / KMAX;
TILES_I:
    for (int i = 0; i < MT; ++i) {
#pragma HLS LOOP_TRIPCOUNT max = 4
    TILES_J:
        for (int j = 0; j < NT; ++j) {
#pragma HLS LOOP_TRIPCOUNT max = 4
            t.c_addr = d.c_base + (ap_uint<32>)(i * NT + j) * WC;
            t.status = wf_linalg::OK;
            t.last = (i == MT - 1 && j == NT - 1) ? 1 : 0;
            t.empties = 0;
            t.template write_stream<64>(tile_out);
            f.a_addr = d.a_base + (ap_uint<32>)(i * P) * WA;
            f.b_addr = d.b_base + (ap_uint<32>)j * WB;
            f.b_step = (ap_uint<32>)NT * WB;
            f.npanels = P;
            f.refused = 0;
            f.template write_stream<64>(feed_out);
        }
    }
}

template <int WBW, int MMAX, int KMAX, int NMAX, int PMAX, int WA, int WB, int WC>
static void systolic_engine_feed_task(hls::stream<ap_uint<64> >& feed_in,
                                      hls::burst_maxi<ap_uint<WBW> > m_mem,
                                      hls::stream<streamutils::framed_word<WBW> >& m_out) {
    EngineFeed f;
    f.template read_stream<64>(feed_in);
    if (f.refused) return;
    EngineCmd c;
    c.tx_id = f.tx_id;
    c.op = f.op;
    c.keep_a = 0;
    c.m = MMAX;
    c.kp = KMAX;
    c.n = NMAX;
    c.npanels = f.npanels;
    c.nb = 1;
    streamutils::write_msg<WBW>(c, m_out);
    const int P = f.npanels;
    // Every panel's read requested up front (hls::burst_maxi), in the order the engine takes them,
    // then all their words in ONE loop: the requests queue in the m_axi adapter, so the port does not
    // wait between panels.  (A burst loop per panel idled the port ~12 cycles at each switch between
    // loops, 8 a tile; one flattened loop with a computed address read a single beat every two cycles;
    // A's panels then B's as two runs waited behind the core's last panel --
    // plans/systolic_characterization.md.)
    ap_uint<32> a = f.a_addr, b = f.b_addr;
REQ:
    for (int p = 0; p < P; ++p) {
#pragma HLS LOOP_TRIPCOUNT max = PMAX
        m_mem.read_request(a, WA);
        m_mem.read_request(b, WB);
        a += WA;
        b += f.b_step;
    }
    const int total = P * (WA + WB);
    ap_uint<16> w = 0;
    bool is_a = true;
WORDS:
    for (int i = 0; i < total; ++i) {
#pragma HLS LOOP_TRIPCOUNT max = PMAX * (WA + WB)
#pragma HLS PIPELINE II = 1
        const bool last = w == (is_a ? WA : WB) - 1;
        streamutils::write_boundary_word<streamutils::framed_word<WBW>, WBW>(m_out, m_mem.read(), last);
        if (last) {
            w = 0;
            is_a = !is_a;
        } else {
            ++w;
        }
    }
}

template <int WBW, int MMAX, int KMAX, int NMAX, int PMAX, int WA, int WB, int WC>
static void systolic_engine_writer_task(hls::stream<ap_uint<64> >& tile_in,
                                        hls::stream<streamutils::framed_word<WBW> >& c_in,
                                        hls::stream<streamutils::framed_word<64> >& st_in,
                                        hls::stream<streamutils::framed_word<WBW> >& w_out) {
    EngineTile t;
    t.template read_stream<64>(tile_in);
    EngineDone done;
    done.tx_id = t.tx_id;
    done.status = t.status;
    MemWCmd c;
    if (t.status != wf_linalg::OK) {  // a refused job: its done, no tile
        c.addr = 0;
        c.len = 0;
        c.fwd_bursts = 1;
        streamutils::write_msg<WBW>(c, w_out);
        streamutils::write_msg<WBW>(done, w_out);
        return;
    }
    EngineResp resp;
    streamutils::read_msg<WBW>(resp, c_in);
    c.addr = t.c_addr;
    c.len = WC;
    c.fwd_bursts = t.last;
    streamutils::write_msg<WBW>(c, w_out);
    if (t.last) streamutils::write_msg<WBW>(done, w_out);
FWD:
    for (int k = 0; k < WC; ++k) {
#pragma HLS PIPELINE II = 1
        bool last;
        const ap_uint<WBW> v =
            streamutils::read_boundary_word<streamutils::framed_word<WBW>, WBW>(c_in, last);
        streamutils::write_boundary_word<streamutils::framed_word<WBW>, WBW>(w_out, v, last);
    }
    EngineStatus st;
    streamutils::read_msg<64>(st, st_in);
}

#endif  // WF_SYSTOLIC_ENGINE_MEM_TASK_H
