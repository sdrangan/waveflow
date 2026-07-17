#ifndef WAVEFLOW_BUILD_CMD_RX_TASK_H
#define WAVEFLOW_BUILD_CMD_RX_TASK_H
// cmd_rx_task.h — stage 1 of the canonical six-stage interleaver (the per-job token source).  A
// pure-stream, single-firing hls::task body: read one InterleaverCmd token off the s_cmd AXIS
// boundary and forward it downstream.  The token ({n, p_off, x_off, y_off}, element coordinates) is
// threaded through every stage so each tile is paced to one job in flight (the sob3 pattern that
// breaks the done==#tasks+1 deadlock).  Copied verbatim by MemStreamStep, instantiated <MEM_DW>.
#include "hls_stream.h"
#include <ap_int.h>
#include "il_cmd.h"

template <int MEM_DW>
static void cmd_rx_task(hls::stream<ap_uint<MEM_DW> >& s_cmd,
                        hls::stream<ap_uint<MEM_DW> >& cmd_out) {
    InterleaverCmd c;
    c.read_stream<MEM_DW>(s_cmd);
    c.write_stream<MEM_DW>(cmd_out);        // forward the token
}

#endif  // WAVEFLOW_BUILD_CMD_RX_TASK_H
