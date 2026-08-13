#ifndef WAVEFLOW_GEN_RF_PASS_THROUGH_TASK_H
#define WAVEFLOW_GEN_RF_PASS_THROUGH_TASK_H
// rf_pass_through_task.h -- GENERATED from RfSampPassThrough.run_iter by waveflow (build/hwgen.py::task_files_to_str).  DO NOT EDIT: regenerate instead.
// A single firing = one hls::task invocation; the runtime re-fires it, so there is no
// command loop here and no INTERFACE pragma (the composite top owns the interface).
// The @synthesizable hook bodies are HAND-WRITTEN and are not lowered from the Python.
#include "hls_stream.h"
#include <ap_int.h>
#include "u_int64_array.h"

static void rf_pass_through_task(
    hls::stream<ap_uint<64> >& s_in,
    hls::stream<ap_uint<64> >& s_out
) {
    UInt64Array blk;
    blk.read_stream<64>(s_in);
    blk.write_stream<64>(s_out);
}

#endif  // WAVEFLOW_GEN_RF_PASS_THROUGH_TASK_H
