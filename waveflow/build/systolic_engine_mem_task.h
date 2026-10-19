// systolic_engine_mem_task.h -- the systolic engine behind memory: the sequencer and the writer.
//
// systolic_engine_seq_task: one job per firing (EngineJobDesc: C = q(A B) with M x K x N in memory).
// Validates it, then runs the output-stationary loop: per C tile (i, j) of MMAX x NMAX, the engine
// command of P = K / KMAX panels -- on the memory reader's command stream as
//   MemRCmd(A(i,0), fwd 1) | EngineCmd | MemRCmd(B(0,j)) | MemRCmd(A(i,1)) | MemRCmd(B(1,j)) | ...
// (the reader relays the EngineCmd ahead of A(i,0)) -- and the tile's word to the writer: its C address,
// whether it is the job's last, and the 2P - 1 empty writes that balance its 2P reads (the m_axi pointer
// FIFOs of one generated top couple the reader's and the writer's firing counts).  A refused job: one
// empty read, and its status to the writer.  Block-major memory: A(i,p) at a_base + (i P + p) WA,
// B(p,j) at b_base + (p NT + j) WB, C(i,j) at c_base + (i NT + j) WC.
//
// systolic_engine_writer_task: one tile (or refused job) per firing.  Takes the engine's Resp and C
// and its Status, and writes MemWCmd(C tile, fwd = last) | [EngineDone] | C, then the empty writes.
// Python twins: waveflow.linalg.systolic_engine_mem.EngineSeq / EngineWriter.
#ifndef WF_SYSTOLIC_ENGINE_MEM_TASK_H
#define WF_SYSTOLIC_ENGINE_MEM_TASK_H
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "wf_linalg_msg.h"
#include "mem_r_cmd.h"
#include "mem_w_cmd.h"
#include "wf_engine_cmd.h"
#include "wf_engine_resp.h"
#include "wf_engine_status.h"
#include "wf_engine_job_desc.h"
#include "wf_engine_done.h"
#include "wf_engine_tile.h"

template <int WBW, int MMAX, int KMAX, int NMAX, int PMAX, int WA, int WB, int WC, typename IN_T>
static void systolic_engine_seq_task(hls::stream<IN_T>& s_job,
                                     hls::stream<streamutils::framed_word<WBW> >& cmd_out,
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
    t.tx_id = d.tx_id;
    if (st != wf_linalg::OK) {
        t.c_addr = 0;
        t.status = st;
        t.last = 1;
        t.empties = 0;
        t.template write_stream<64>(tile_out);
        MemRCmd r;  // one empty read for the done's one write
        r.addr = 0;
        r.len = 0;
        r.fwd_bursts = 0;
        streamutils::write_msg<WBW>(r, cmd_out);
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
            t.empties = 2 * P - 1;
            t.template write_stream<64>(tile_out);
        PANELS:
            for (int p = 0; p < P; ++p) {
#pragma HLS LOOP_TRIPCOUNT max = PMAX
                MemRCmd ra;
                ra.addr = d.a_base + (ap_uint<32>)(i * P + p) * WA;
                ra.len = WA;
                ra.fwd_bursts = (p == 0) ? 1 : 0;
                streamutils::write_msg<WBW>(ra, cmd_out);
                if (p == 0) {
                    EngineCmd c;
                    c.tx_id = d.tx_id;
                    c.op = op;
                    c.keep_a = 0;
                    c.m = MMAX;
                    c.kp = KMAX;
                    c.n = NMAX;
                    c.npanels = P;
                    c.nb = 1;
                    streamutils::write_msg<WBW>(c, cmd_out);
                }
                MemRCmd rb;
                rb.addr = d.b_base + (ap_uint<32>)(p * NT + j) * WB;
                rb.len = WB;
                rb.fwd_bursts = 0;
                streamutils::write_msg<WBW>(rb, cmd_out);
            }
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
EMPTIES:
    for (int e = 0; e < t.empties; ++e) {
#pragma HLS LOOP_TRIPCOUNT max = 2 * PMAX
        MemWCmd z;
        z.addr = 0;
        z.len = 0;
        z.fwd_bursts = 0;
        streamutils::write_msg<WBW>(z, w_out);
    }
}

#endif  // WF_SYSTOLIC_ENGINE_MEM_TASK_H
