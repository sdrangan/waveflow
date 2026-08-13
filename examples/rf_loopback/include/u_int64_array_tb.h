#ifndef INCLUDE_U_INT64_ARRAY_TB_H
#define INCLUDE_U_INT64_ARRAY_TB_H

#include <cctype>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>
#include "streamutils_tb.h"

#define WAVEFLOW_ENABLE_U_INT64_ARRAY_TB_H_MEMBERS
#include "u_int64_array.h"
#undef WAVEFLOW_ENABLE_U_INT64_ARRAY_TB_H_MEMBERS

inline void UInt64Array::dump_json(std::ostream& os, int indent, int level) const {
    const int step = (indent < 0) ? 0 : indent;
    os << "[";
    for (int i0 = 0; i0 < 64; ++i0) {
    if (i0 > 0) { os << ","; }
    os << static_cast<unsigned long long>(this->data[i0]);
    }
    os << "]";
}

inline void UInt64Array::load_json(const std::string& json_text, size_t& pos) {
    streamutils::json_expect_char(json_text, pos, '[');
    for (int i0 = 0; i0 < 64; ++i0) {
    if (i0 > 0) {
        streamutils::json_expect_char(json_text, pos, ',');
    }
    this->data[i0] = static_cast<ap_uint<64>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    streamutils::json_expect_char(json_text, pos, ']');
}

inline void UInt64Array::load_json(std::istream& is) {
    std::string json_text((std::istreambuf_iterator<char>(is)), std::istreambuf_iterator<char>());
    size_t pos = 0;
    streamutils::json_skip_ws(json_text, pos);
    this->load_json(json_text, pos);
    streamutils::json_skip_ws(json_text, pos);
    if (pos != json_text.size()) {
        throw std::runtime_error("Trailing characters after JSON object.");
    }
}

inline void UInt64Array::dump_json_file(const char* file_path, int indent) const {
    std::ofstream ofs(file_path);
    if (!ofs) {
        throw std::runtime_error("Failed to open output JSON file.");
    }
    this->dump_json(ofs, indent);
}

inline void UInt64Array::load_json_file(const char* file_path) {
    std::ifstream ifs(file_path);
    if (!ifs) {
        throw std::runtime_error("Failed to open input JSON file.");
    }
    this->load_json(ifs);
}

#endif // INCLUDE_U_INT64_ARRAY_TB_H