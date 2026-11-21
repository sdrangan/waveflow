#ifndef VMUL_ISSUE_TASK_H
#define VMUL_ISSUE_TASK_H
// vmul_issue_task.h -- HAND-WRITTEN.  The HLS twin of vmul.py::VmulIssue.run_iter.
//
// One firing = one command: read a VmulCmd off s_cmd, ask the x reader and the y reader for n
// elements each (MemRCmd, in words), then pass the command on to the multiply stage.  It never waits
// for the data it asked for -- the readers send it to vmul_mul_task -- so there is no write followed by
// a blocking read in this task.
//
// An error is fatal: n == 0 asks for nothing, passes the command on (the multiply stage answers
// BAD_LEN through the writer) and halts until a reset.
#include "hls_stream.h"
#include <ap_int.h>
#include "streamutils_hls.h"
#include "int16_array_utils.h"
#include "vmul_cmd.h"
#include "mem_r_cmd.h"

template <int W>
static void vmul_issue_task(hls::stream<streamutils::axi4s_word<W> >& s_cmd,
                            hls::stream<streamutils::framed_word<W> >& m_rx,
                            hls::stream<streamutils::framed_word<W> >& m_ry,
                            hls::stream<streamutils::framed_word<W> >& m_desc) {
    static bool halted = false;                  // set by an error, cleared only by a reset
#pragma HLS reset variable=halted
    if (halted) {
        return;
    }
    VmulCmd cmd;
    cmd.read_axi4_stream<W>(s_cmd);              // a blocking read first
    const int n = (int)cmd.n;
    if (n == 0) {
        cmd.write_framed_stream<W>(m_desc);
        halted = true;
        return;
    }
    MemRCmd rc;
    rc.len = int16_array_utils::get_nwords<W>(n);
    rc.fwd_bursts = 0;
    rc.addr = cmd.x_addr;
    rc.write_framed_stream<W>(m_rx);
    rc.addr = cmd.y_addr;
    rc.write_framed_stream<W>(m_ry);
    cmd.write_framed_stream<W>(m_desc);
}

#endif  // VMUL_ISSUE_TASK_H
