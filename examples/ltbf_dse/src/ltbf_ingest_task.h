#ifndef EXAMPLES_LTBF_DSE_INGEST_TASK_H
#define EXAMPLES_LTBF_DSE_INGEST_TASK_H
// ltbf_ingest_task.h -- the Ingest DMA (HAND-WRITTEN), the HLS twin of
// examples/ltbf_dse/hw/ingest.py IngestDma.run_iter (plans/ltbf_dse/ltbf_dse.md step 5.1; spec §5.4).
//
// ONE FIRING THAT NEVER RETURNS, as the Python reads (the state is loop-carried, not static):
//   1. the configuration (IngestCfg) once, and a newer one between symbols;
//   2. the symbol's first word (blocking), then a short drain of the credit stream to its newest
//      value (the view hands it over a cycle after the FIFO has room), and a non-blocking read
//      every cycle of the loops after;
//   3. without n_chunks free ring slots: drop the symbol (read its NANT bursts) and count it;
//   4. else per group of KMAX antennas: the corner turn into buf, then every chunk's panels (p, j) as
//      in-band MemWCmd writes of WB words at slot + (p·J + j)·WB, the last panel of each chunk
//      carrying the chunk's Resp (src 2) as its forwarded burst;
//   5. the status (IngestStatus{chunks, overruns}).
// Data words move through streamutils; records through their generated schemas.
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "mem_w_cmd.h"
#include "ltbf_resp.h"
#include "ltbf_ingest_cfg.h"
#include "ltbf_ingest_status.h"

template <int W, int LB, int KMAX, int NMAX, int NANT, int J, int NSC, typename SYM_T, typename CFG_T,
          typename CRD_T, typename WOUT_T, typename ST_T>
static void ltbf_ingest_task(hls::stream<SYM_T>& s_sym, hls::stream<CFG_T>& s_cfg,
                             hls::stream<CRD_T>& s_crd, hls::stream<WOUT_T>& m_w,
                             hls::stream<ST_T>& m_status) {
    const int EPW = W / (2 * LB);                       // complex elements a word
    const int ANTW = (NSC + EPW - 1) / EPW;             // an antenna's words
    const int NCH = (NSC + J * NMAX - 1) / (J * NMAX);  // chunks a symbol (the last zero-padded)
    const int P = NANT / KMAX;
    const int WB = (KMAX * NMAX + EPW - 1) / EPW;       // a panel's words
    const int NW = NMAX / EPW;                          // a panel row's words
    const int COLS = NCH * J * NW;
    static_assert(NMAX % EPW == 0 && NSC % EPW == 0 && NANT % KMAX == 0, "word-aligned panels");
    static ap_uint<W> buf[KMAX][COLS];  // the corner-turn buffer; words past ANTW stay zero
    IngestCfg cfg;
    streamutils::read_msg<W>(cfg, s_cfg);
    // One firing that never returns, as the Python twin's run_iter: the DMA's state is carried by
    // this loop, not kept in statics across firings (Vitis HLS 2024.1's free-running task read such
    // statics one firing stale: XSI showed Resps and ring slots a symbol behind).
    ap_uint<16> credit = 0, taken = 0, seq = 0;
    ap_uint<32> chunks = 0, overruns = 0;
SYMBOLS:
    for (;;) {
        bool tl;
        const ap_uint<W> first = streamutils::read_boundary_word<SYM_T, W>(s_sym, tl);  // a symbol
        if (!s_cfg.empty()) streamutils::read_msg<W>(cfg, s_cfg);
        // The newest credit: the view hands it over a cycle after the FIFO has room: drain a few.
    DRAIN:
        for (int i = 0; i < 4; ++i) {
#pragma HLS PIPELINE II = 1
            CRD_T c;
            if (s_crd.read_nb(c)) credit = c.data(15, 0);
        }
        const ap_uint<16> used = taken - credit;
        const int free = (int)cfg.ring_depth - (int)used;
        if (cfg.enable == 0 || free < NCH) {
        DROP:
            for (int k = 1; k < NANT * ANTW; ++k) {
#pragma HLS PIPELINE II = 1
                streamutils::read_boundary_word<SYM_T, W>(s_sym, tl);
            }
            if (cfg.enable != 0) {
                overruns += 1;
                seq += NCH;
                IngestStatus status;
                status.chunks = chunks;
                status.overruns = overruns;
                streamutils::write_msg<W>(status, m_status);
            }
            continue;
        }
    GROUPS:
        for (int p = 0; p < P; ++p) {
        TURN:
            for (int k = 0; k < KMAX * ANTW; ++k) {
#pragma HLS PIPELINE II = 1
                const int a = k / ANTW, w = k % ANTW;
                ap_uint<W> v = first;
                if (p != 0 || k != 0) v = streamutils::read_boundary_word<SYM_T, W>(s_sym, tl);
                buf[a][w] = v;
                CRD_T c;
                if (s_crd.read_nb(c)) credit = c.data(15, 0);
            }
        CHUNKS:
            for (int c = 0; c < NCH; ++c) {
                const ap_uint<16> slot = (ap_uint<16>)(taken + c) % (ap_uint<16>)cfg.ring_depth;
                const ap_uint<32> base = cfg.ring_base + (ap_uint<32>)slot * cfg.chunk_words;
            TILES:
                for (int j = 0; j < J; ++j) {
                    const bool last = (p == P - 1) && (j == J - 1);
                    MemWCmd cmd;
                    cmd.addr = base + (ap_uint<32>)(p * J + j) * WB;
                    cmd.len = WB;
                    cmd.fwd_bursts = last ? 1 : 0;
                    streamutils::write_msg<W>(cmd, m_w);
                    if (last) {
                        Resp r;
                        r.src = 2;
                        r.status = 0;
                        r.tx_id = 0;
                        r.seq = seq + c;
                        streamutils::write_msg<W>(r, m_w);
                    }
                PANEL:
                    for (int k = 0; k < KMAX * NW; ++k) {
#pragma HLS PIPELINE II = 1
                        const int a = k / NW, w = k % NW;
                        streamutils::write_boundary_word<WOUT_T, W>(
                            m_w, buf[a][(c * J + j) * NW + w], k == KMAX * NW - 1);
                        CRD_T cr;
                        if (s_crd.read_nb(cr)) credit = cr.data(15, 0);
                    }
                }
            }
        }
        seq += NCH;
        taken += NCH;
        chunks += NCH;
        IngestStatus status;
        status.chunks = chunks;
        status.overruns = overruns;
        streamutils::write_msg<W>(status, m_status);
    }
}

#endif  // EXAMPLES_LTBF_DSE_INGEST_TASK_H
