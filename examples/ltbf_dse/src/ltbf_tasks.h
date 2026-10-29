#ifndef EXAMPLES_LTBF_DSE_TASKS_H
#define EXAMPLES_LTBF_DSE_TASKS_H
// ltbf_tasks.h -- examples/ltbf_dse's small kernels (HAND-WRITTEN), the HLS twins of their run_iter
// (plans/ltbf_dse/ltbf_dse.md step 5.1; spec §5.4).  One firing = one message, written as the Python
// reads (docs/guide/vectorization/hls/loop_optimization.md: a loop per message); every record moves
// through its generated schema, every data word through streamutils.  Each framed port's word type is
// a template parameter of its own, in port order (the generated top passes one a framed port): an
// internal FIFO carries framed_word, a top-level port axi4s_word.
//
//   ltbf_sys_tap_task    examples/ltbf_dse/hw/sys_wrap.py  SysTap
//   ltbf_cfg_sink_task   SysCtrl / VecCtrl (the Ctrl's configuration, held)
//   ltbf_resp_hub*_task  examples/ltbf_dse/hw/resp_hub.py  RespHub (1 or 3 sources)
//   ltbf_vec_decoder_task, ltbf_vec_writer_task  examples/ltbf_dse/hw/vec_wrap.py
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "mem_r_cmd.h"
#include "mem_w_cmd.h"
#include "wf_engine_done.h"
#include "wf_vec_resp.h"
#include "wf_vec_status.h"
#include "ltbf_resp.h"
#include "ltbf_sys_cfg.h"
#include "ltbf_sys_status.h"
#include "ltbf_vec_mem_job.h"
#include "ltbf_store_plan.h"

namespace ltbf {
static const int SRC_SYS = 0, SRC_VEC = 1, SRC_INGEST = 2;
static const int MAX_LD = 4, MAX_ST = 4;
}  // namespace ltbf

// SysTap: EngineDone in; a Resp (src 0) to the RespHub and the Ctrl's status out.  One firing a job.
template <int W, typename ST_T>
static void ltbf_sys_tap_task(hls::stream<ap_uint<W> >& s_done, hls::stream<ap_uint<W> >& m_resp,
                              hls::stream<ST_T>& m_status) {
    static ap_uint<32> jobs_done = 0;
    EngineDone d;
    d.template read_stream<W>(s_done);  // the memory writer's done port: plain words
    jobs_done += 1;
    Resp r;
    r.src = ltbf::SRC_SYS;
    r.status = d.status;
    r.tx_id = d.tx_id;
    r.seq = 0;
    r.template write_stream<W>(m_resp);
    SysStatus st;
    st.jobs_done = jobs_done;
    st.last_tx_id = d.tx_id;
    st.last_status = d.status;
    streamutils::write_msg<W>(st, m_status);
}

// SysCtrl / VecCtrl: the Ctrl's configuration (SysCfg{enable}), taken and held.  One firing a write.
template <int W, typename CFG_T>
static void ltbf_cfg_sink_task(hls::stream<CFG_T>& s_cfg) {
    static ap_uint<32> enable = 1;
    SysCfg c;
    streamutils::read_msg<W>(c, s_cfg);
    enable = c.enable;
}

// RespHub, one source: a word through.
template <int W>
static void ltbf_resp_hub1_task(hls::stream<ap_uint<W> >& s_in0, hls::stream<ap_uint<W> >& m_out) {
    m_out.write(s_in0.read());
}

// RespHub, three sources: round-robin, one source tried a firing (a record is one word, so records
// never interleave).  The firing never blocks on an empty source.
template <int W>
static void ltbf_resp_hub3_task(hls::stream<ap_uint<W> >& s_in0, hls::stream<ap_uint<W> >& s_in1,
                                hls::stream<ap_uint<W> >& s_in2, hls::stream<ap_uint<W> >& m_out) {
#pragma HLS PIPELINE II = 1
    static ap_uint<2> k = 0;
    ap_uint<W> w;
    bool got = false;
    if (k == 0) {
        got = s_in0.read_nb(w);
    } else if (k == 1) {
        got = s_in1.read_nb(w);
    } else {
        got = s_in2.read_nb(w);
    }
    if (got) m_out.write(w);
    k = (k == 2) ? ap_uint<2>(0) : ap_uint<2>(k + 1);
}

// VecDecoder: a VecMemJob in; a MemRCmd to each program reader, the store plan to the writer, then
// the data reader's commands (count rounds, each load stream once a round).  One firing a job.
template <int W, typename JOB_T, typename LC_T, typename XC_T, typename SC_T, typename DC_T>
static void ltbf_vec_decoder_task(hls::stream<JOB_T>& s_job, hls::stream<LC_T>& m_lcmd,
                                  hls::stream<XC_T>& m_xcmd, hls::stream<SC_T>& m_scmd,
                                  hls::stream<DC_T>& m_dcmd, hls::stream<ap_uint<W> >& m_plan) {
    VecMemJob j;
    streamutils::read_msg<W>(j, s_job);
    MemRCmd c;
    c.fwd_bursts = 0;
    c.addr = j.l_addr;
    c.len = j.l_words;
    streamutils::write_msg<W>(c, m_lcmd);
    c.addr = j.x_addr;
    c.len = j.x_words;
    streamutils::write_msg<W>(c, m_xcmd);
    c.addr = j.s_addr;
    c.len = j.s_words;
    streamutils::write_msg<W>(c, m_scmd);
    StorePlan p;
    p.tx_id = j.tx_id;
    p.count = j.count;
    p.n_st = j.n_st;
    p.st0_base = j.st0_base; p.st0_stride = j.st0_stride; p.st0_words = j.st0_words;
    p.st1_base = j.st1_base; p.st1_stride = j.st1_stride; p.st1_words = j.st1_words;
    p.st2_base = j.st2_base; p.st2_stride = j.st2_stride; p.st2_words = j.st2_words;
    p.st3_base = j.st3_base; p.st3_stride = j.st3_stride; p.st3_words = j.st3_words;
    p.template write_stream<W>(m_plan);
    const ap_uint<32> base[ltbf::MAX_LD] = {j.ld0_base, j.ld1_base, j.ld2_base, j.ld3_base};
    const ap_uint<32> stride[ltbf::MAX_LD] = {j.ld0_stride, j.ld1_stride, j.ld2_stride, j.ld3_stride};
    const ap_uint<32> words[ltbf::MAX_LD] = {j.ld0_words, j.ld1_words, j.ld2_words, j.ld3_words};
ROUNDS:
    for (ap_uint<16> i = 0; i < j.count; ++i) {
    STREAMS:
        for (int k = 0; k < ltbf::MAX_LD; ++k) {
#pragma HLS PIPELINE II = 1
            if (k < (int)j.n_ld) {
                MemRCmd d;
                d.addr = base[k] + i * stride[k];
                d.len = words[k];
                d.fwd_bursts = 0;
                streamutils::write_msg<W>(d, m_dcmd);
            }
        }
    }
}

// VecWriter: per store, the VecResp and its data burst in; an in-band MemWCmd at the stream's next
// address, then the data.  After the job's last store, its VecStatus, then a zero-length MemWCmd whose
// forwarded burst is the job's Resp (src 1): the memory writer echoes it only after the stores before
// it are in memory.  Then the Ctrl's status.  (Waiting for the status before the last store's data would deadlock: the unit
// reports its status only once all its data has left.)
template <int W, typename OUT_T, typename ST_T, typename WOUT_T, typename CST_T>
static void ltbf_vec_writer_task(hls::stream<ap_uint<W> >& s_plan, hls::stream<OUT_T>& s_out,
                                 hls::stream<ST_T>& s_status, hls::stream<WOUT_T>& m_w,
                                 hls::stream<CST_T>& m_status) {
    static ap_uint<32> jobs_done = 0;
    StorePlan p;
    p.template read_stream<W>(s_plan);
    const ap_uint<32> base[ltbf::MAX_ST] = {p.st0_base, p.st1_base, p.st2_base, p.st3_base};
    const ap_uint<32> stride[ltbf::MAX_ST] = {p.st0_stride, p.st1_stride, p.st2_stride, p.st3_stride};
    const ap_uint<32> words[ltbf::MAX_ST] = {p.st0_words, p.st1_words, p.st2_words, p.st3_words};
    MemWCmd c;
ROUNDS:
    for (ap_uint<16> i = 0; i < p.count; ++i) {
    STORES:
        for (int k = 0; k < ltbf::MAX_ST; ++k) {
            if (k >= (int)p.n_st) break;
            VecResp resp;
            streamutils::read_msg<W>(resp, s_out);
            c.addr = base[k] + i * stride[k];
            c.len = words[k];
            c.fwd_bursts = 0;
            streamutils::write_msg<W>(c, m_w);
        DATA:
            for (ap_uint<32> n = 0; n < words[k]; ++n) {
#pragma HLS PIPELINE II = 1
                bool tl;
                const ap_uint<W> v = streamutils::read_boundary_word<OUT_T, W>(s_out, tl);
                streamutils::write_boundary_word<WOUT_T, W>(m_w, v, tl);
            }
        }
    }
    VecStatus st;
    streamutils::read_msg<64>(st, s_status);
    Resp done;
    done.src = ltbf::SRC_VEC;
    done.status = st.status;
    done.tx_id = p.tx_id;
    done.seq = 0;
    c.addr = 0;
    c.len = 0;
    c.fwd_bursts = 1;
    streamutils::write_msg<W>(c, m_w);
    streamutils::write_msg<W>(done, m_w);
    jobs_done += 1;
    SysStatus cs;  // the Ctrl's status, after every job
    cs.jobs_done = jobs_done;
    cs.last_tx_id = p.tx_id;
    cs.last_status = st.status;
    streamutils::write_msg<W>(cs, m_status);
}

#endif  // EXAMPLES_LTBF_DSE_TASKS_H
