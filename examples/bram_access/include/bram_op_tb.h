#ifndef INCLUDE_BRAM_OP_TB_H
#define INCLUDE_BRAM_OP_TB_H

#include <cctype>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>
#include "streamutils_tb.h"

#define WAVEFLOW_ENABLE_BRAM_OP_TB_H_MEMBERS
#include "bram_op.h"
#undef WAVEFLOW_ENABLE_BRAM_OP_TB_H_MEMBERS

inline const char* enum_to_string(BramOp value) {
    switch (value) {
    case BramOp::WRITE:
        return "WRITE";
    case BramOp::COMPUTE:
        return "COMPUTE";
    default:
        return "UNKNOWN";
    }
}

#endif // INCLUDE_BRAM_OP_TB_H