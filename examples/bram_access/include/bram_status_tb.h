#ifndef INCLUDE_BRAM_STATUS_TB_H
#define INCLUDE_BRAM_STATUS_TB_H

#include <cctype>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>
#include "streamutils_tb.h"

#define WAVEFLOW_ENABLE_BRAM_STATUS_TB_H_MEMBERS
#include "bram_status.h"
#undef WAVEFLOW_ENABLE_BRAM_STATUS_TB_H_MEMBERS

inline const char* enum_to_string(BramStatus value) {
    switch (value) {
    case BramStatus::OK:
        return "OK";
    case BramStatus::OUT_OF_RANGE:
        return "OUT_OF_RANGE";
    default:
        return "UNKNOWN";
    }
}

#endif // INCLUDE_BRAM_STATUS_TB_H