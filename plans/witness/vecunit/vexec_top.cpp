// vexec_top.cpp -- the witness top (witnesses 1-4): one job per call, queues as plain arrays.
#include "cfg.h"
#include "vexec.h"

typedef vx::Exec<vx::Cfg> E;

void vexec_top(hls::stream<vx::word_t>& prog, const E::lg_t qin[E::NQI][E::QD],
               E::lg_t qout[E::NQO][E::QD], hls::stream<vx::word_t>& status) {
#pragma HLS INTERFACE mode=ap_fifo port=prog
#pragma HLS INTERFACE mode=ap_fifo port=status
    E::run(prog, qin, qout, status);
}

// The same code, not synthesized: the testbench's reference for C/RTL co-simulation.
void vexec_ref(hls::stream<vx::word_t>& prog, const E::lg_t qin[E::NQI][E::QD],
               E::lg_t qout[E::NQO][E::QD], hls::stream<vx::word_t>& status) {
    E::run(prog, qin, qout, status);
}
