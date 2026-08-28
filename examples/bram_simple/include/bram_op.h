#ifndef INCLUDE_BRAM_OP_H
#define INCLUDE_BRAM_OP_H

#include <ap_int.h>
#include <hls_stream.h>
#if __has_include(<hls_axi_stream.h>)
#include <hls_axi_stream.h>
#else
#include <ap_axi_sdata.h>
#endif
#include "streamutils_hls.h"

enum class BramOp {
    WRITE = 0,
    COMPUTE = 1,
};

#endif // INCLUDE_BRAM_OP_H