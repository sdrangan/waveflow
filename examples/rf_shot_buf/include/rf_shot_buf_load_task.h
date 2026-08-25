#ifndef WAVEFLOW_RF_SHOT_BUF_LOAD_TASK_H
#define WAVEFLOW_RF_SHOT_BUF_LOAD_TASK_H
// rf_shot_buf_load_task.h — fill the shot buffer with ONE shot, then say so.
//
// The finite buffer's writer (plans/rf_shot_buf.md, Stage A).  Its Python twin is
// RfShotBufLoad.run_iter, which is the pysim golden and NOT the source of this file.
//
// THE SHAPE IS `while (1)` AROUND A COUNTED LOOP, AND BOTH HALVES ARE LOAD-BEARING.
//
// The inner loop is counted (`i < NW`) with `PIPELINE II=1`, so there is no data-dependent spin for
// Vitis to refuse to flatten.  That is the difference from rf_samp_buf_capture_task.h, whose per-word
// loop contains an inner `while` waiting for the ingress and which is therefore pinned at II=2:
//
//     [HLS 200-960] Cannot flatten loop ... sub loop is do-while
//
// A shot buffer has nothing to wait for mid-shot -- the reader is not live -- so the wait that costs
// the streaming design its II does not exist here.  That is the concurrency argument, expressed as a
// loop shape rather than as prose.
//
// The outer `while (1)` is what keeps the task alive across shots.  A boundary between firings costs
// 3 cycles (measured in plans/witness/task_loop/), and putting the shot boundary on the OUTER loop
// pays that once per shot instead of once per word.
//
// NO `static`, AND THAT IS DELIBERATE.
//
// The alternative shape -- one word per firing at a running pointer -- needs a `static` write
// pointer, and a `static` in an hls::task is the reset trap that cost examples/rf_blk_delay a day:
// a task that WRITES before it READS counts during reset, and `= 0` on a static is a simulation
// initial value, not a reset.  Here the address IS the loop index, so there is no state to reset and
// no pragma to remember.
//
// THE NEVER-STALL LAW DOES NOT APPLY TO THIS TASK.
//
// rf_samp_buf_ingress_task.h may have exactly one blocking call because a converter cannot be
// back-pressured.  This task's input is an m_axi arena or a DMA -- something that can be told to
// wait -- so the law would be an obligation inherited rather than measured.  Do not copy it here.
#include "hls_stream.h"
#include <ap_int.h>

/// @tparam W   word width in bits.
/// @tparam N   buffer depth in WORDS -- the ARRAY SIZE.  `mode=bram` on an unsized pointer silently
///             degrades to an ap_vld scalar port, so this number is what makes the pragma take
///             effect; it is not decoration.
/// @tparam NW  words in one shot (NW <= N).
template <int W, int N, int NW>
static void rf_shot_buf_load_task(ap_uint<W> buf_w[N], hls::stream<ap_uint<W> >& s_in,
                                  hls::stream<ap_uint<W> >& rdy_out) {
    while (1) {
    load_shot:
        for (int i = 0; i < NW; i++) {
#pragma HLS PIPELINE II=1
            buf_w[i] = s_in.read();       // a BRAM port: no handshake, nothing to refuse the write
        }
        // ONE token per shot, and it is the only thing the two tasks ever say to each other.  A
        // blocking write is correct here (contrast the RX ingress's write_nb): this token is not a
        // running position of which only the newest matters -- it is the statement that the memory
        // now holds a complete shot, and dropping it would leave the reader waiting forever.
        rdy_out.write((ap_uint<W>)1);
    }
}

#endif  // WAVEFLOW_RF_SHOT_BUF_LOAD_TASK_H
