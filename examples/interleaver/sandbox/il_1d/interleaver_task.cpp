// interleaver_task.cpp — ILLUSTRATIVE Phase-5 target: the free-running interleaver as an hls::task
// network (DTLP), contrasted with the bounded DATAFLOW form (interleaver.cpp / C1/C3/C6).
//
// This is the shape the general/component.md model generates. NOT yet compiled/de-risked — it's the
// "what does it look like" reference. What it IS grounded in this session:
//   * hls::task + m_axi csynths and is functionally correct (task_maxi sandbox), verified by XSI/xsim
//     on Windows (Gate G1), NOT Vitis cosim (212-345).
//   * task bodies are SINGLE-FIRING (the runtime re-fires them); no internal while(1).
//   * the DTLP rule: a dedicated read-owner / write-owner task holds each m_axi; compute tasks touch
//     only streams. Random-access gather still needs X buffered resident inside the gather task.
//
// Topology (all edges are hls::stream, so overlap is ELEMENT-level, and it's free-running — no counted
// loop, no fixed njobs; it drains naturally in csim when s_cmd empties, runs forever in HW):
//
//   s_cmd -> load_task ==(x_s,p_s,cmd)==> gather_task ==(y_s,cmd)==> store_task -> s_done
//              |owns in_mem (read)                                       |owns out_mem (write)
//
#include "hls_task.h"
#include "hls_stream.h"
#include <ap_int.h>
#include "memmgr.hpp"
#include "float32_array_utils.h"
#include "int32_array_utils.h"

#ifndef MEM_DW
#define MEM_DW 64
#endif
static const int N_MAX = 1024;

// Command packet: element count + byte addresses (the histogram/shared_mem paradigm).
#include "cmd.h"

// LOAD: sole owner of the read m_axi. One firing per command -> DMA P and X into streams, and forward
// the command downstream so gather/store know n and the Y address.
static void load_task(hls::stream<Cmd>& cmd_in, hls::stream<Cmd>& cmd_fwd,
                      hls::stream<float>& x_s, hls::stream<ap_int<32> >& p_s,
                      const ap_uint<MEM_DW>* in_mem) {
    Cmd c = cmd_in.read();                       // blocks until a job arrives (drains -> csim ends)
    cmd_fwd.write(c);
    const int n  = (int)c.n;
    const int pw = waveflow::memmgr::byte_addr_to_word_index<MEM_DW>(c.p_addr);
    const int xw = waveflow::memmgr::byte_addr_to_word_index<MEM_DW>(c.x_addr);
    ap_int<32> pbuf[N_MAX];  float xbuf[N_MAX];
    int32_array_utils::read_array_slice<MEM_DW>(in_mem + pw, 0, n, pbuf);   // burst read P
    float32_array_utils::read_array_slice<MEM_DW>(in_mem + xw, 0, n, xbuf); // burst read X
    for (int i = 0; i < n; i++) p_s.write(pbuf[i]);
    for (int i = 0; i < n; i++) x_s.write(xbuf[i]);
}

// GATHER: X must be resident for random access, so buffer the whole X stream, then Y[i]=xbuf[P[i]].
static void gather_task(hls::stream<Cmd>& cmd_fwd, hls::stream<Cmd>& cmd_out,
                       hls::stream<float>& x_s, hls::stream<ap_int<32> >& p_s,
                       hls::stream<float>& y_s) {
    Cmd c = cmd_fwd.read();
    cmd_out.write(c);
    const int n = (int)c.n;
    ap_int<32> pbuf[N_MAX];  float xbuf[N_MAX];
    for (int i = 0; i < n; i++) pbuf[i] = p_s.read();
    for (int i = 0; i < n; i++) xbuf[i] = x_s.read();
GY: for (int i = 0; i < n; i++) {
#pragma HLS PIPELINE II=1
        y_s.write(xbuf[(int)pbuf[i]]);           // the gather (compute floor: n cycles)
    }
}

// STORE: sole owner of the write m_axi. Consume Y, DMA out, emit a completion token for the TB.
static void store_task(hls::stream<Cmd>& cmd_out, hls::stream<float>& y_s,
                      hls::stream<ap_uint<32> >& done, ap_uint<MEM_DW>* out_mem) {
    Cmd c = cmd_out.read();
    const int n  = (int)c.n;
    const int yw = waveflow::memmgr::byte_addr_to_word_index<MEM_DW>(c.y_addr);
    float ybuf[N_MAX];
    for (int i = 0; i < n; i++) ybuf[i] = y_s.read();
    float32_array_utils::write_array_slice<MEM_DW>(ybuf, out_mem + yw, 0, n);  // pure-write burst
    done.write(c.n);                              // TB blocking-reads this to sync (m_axi has no ack)
}

// TOP: free-running (ap_ctrl_none). Instantiate the tasks + the streams that wire them. in_mem is a
// read-only master (stable); out_mem the write master. Verified via XSI/xsim, not Vitis cosim.
void interleaver(hls::stream<Cmd>& s_cmd, hls::stream<ap_uint<32> >& s_done,
                 const ap_uint<MEM_DW>* in_mem, ap_uint<MEM_DW>* out_mem) {
#pragma HLS INTERFACE axis port=s_cmd
#pragma HLS INTERFACE axis port=s_done
#pragma HLS INTERFACE m_axi port=in_mem  offset=slave bundle=gmem0 depth=8192
#pragma HLS INTERFACE m_axi port=out_mem offset=slave bundle=gmem1 depth=8192
#pragma HLS INTERFACE ap_ctrl_none port=return
#pragma HLS stable variable=in_mem
    hls_thread_local hls::stream<Cmd>        c_lg, c_gs;
    hls_thread_local hls::stream<float>      x_s, y_s;
    hls_thread_local hls::stream<ap_int<32> > p_s;
    hls_thread_local hls::task t_load (load_task,   s_cmd, c_lg, x_s, p_s, in_mem);
    hls_thread_local hls::task t_gath (gather_task, c_lg, c_gs, x_s, p_s, y_s);
    hls_thread_local hls::task t_store(store_task,  c_gs, y_s, s_done, out_mem);
}
