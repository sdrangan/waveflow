#ifndef INCLUDE_FIR_CFG_TB_H
#define INCLUDE_FIR_CFG_TB_H

#include <cctype>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>
#include "streamutils_tb.h"

#include "int16_array_tb.h"

#define WAVEFLOW_ENABLE_FIR_CFG_TB_H_MEMBERS
#include "fir_cfg.h"
#undef WAVEFLOW_ENABLE_FIR_CFG_TB_H_MEMBERS

inline void FirCfg::dump_json(std::ostream& os, int indent, int level) const {
    const int step = (indent < 0) ? 0 : indent;
    os << "{";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"ntaps\": ";
    os << static_cast<unsigned long long>(this->ntaps);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"apply_at\": ";
    os << static_cast<unsigned long long>(this->apply_at);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"coeffs\": ";
    os << "[";
    for (int i0 = 0; i0 < 16; ++i0) {
    if (i0 > 0) { os << ","; }
    os << static_cast<long long>(this->coeffs.data[i0]);
    }
    os << "]";
    os << "\n";
    for (int i = 0; i < (level) * step; ++i) { os << ' '; }
    os << "}";
}

inline void FirCfg::load_json(const std::string& json_text, size_t& pos) {
    streamutils::json_expect_char(json_text, pos, '{');
    bool seen_root_ntaps = false;
    bool seen_root_apply_at = false;
    bool seen_root_coeffs = false;
    bool first = true;
    while (true) {
    streamutils::json_skip_ws(json_text, pos);
    if (pos < json_text.size() && json_text[pos] == '}') {
        ++pos;
        break;
    }
    if (!first) {
        streamutils::json_expect_char(json_text, pos, ',');
    }
    first = false;
    std::string key = streamutils::json_parse_string(json_text, pos);
    streamutils::json_expect_char(json_text, pos, ':');
    if (key == "ntaps") {
        seen_root_ntaps = true;
        this->ntaps = static_cast<ap_uint<32>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else if (key == "apply_at") {
        seen_root_apply_at = true;
        this->apply_at = static_cast<ap_uint<32>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else if (key == "coeffs") {
        seen_root_coeffs = true;
        streamutils::json_expect_char(json_text, pos, '[');
        for (int i0 = 0; i0 < 16; ++i0) {
            if (i0 > 0) {
                streamutils::json_expect_char(json_text, pos, ',');
            }
            this->coeffs.data[i0] = static_cast<ap_int<16>>(static_cast<long long>(streamutils::json_parse_number(json_text, pos)));
        }
        streamutils::json_expect_char(json_text, pos, ']');
    }
    else {
        throw std::runtime_error("Malformed JSON: unexpected key for schema.");
    }
    }
    if (!seen_root_ntaps) {
    throw std::runtime_error("Malformed JSON: missing required key 'ntaps'.");
    }
    if (!seen_root_apply_at) {
    throw std::runtime_error("Malformed JSON: missing required key 'apply_at'.");
    }
    if (!seen_root_coeffs) {
    throw std::runtime_error("Malformed JSON: missing required key 'coeffs'.");
    }
}

inline void FirCfg::load_json(std::istream& is) {
    std::string json_text((std::istreambuf_iterator<char>(is)), std::istreambuf_iterator<char>());
    size_t pos = 0;
    streamutils::json_skip_ws(json_text, pos);
    this->load_json(json_text, pos);
    streamutils::json_skip_ws(json_text, pos);
    if (pos != json_text.size()) {
        throw std::runtime_error("Trailing characters after JSON object.");
    }
}

inline void FirCfg::dump_json_file(const char* file_path, int indent) const {
    std::ofstream ofs(file_path);
    if (!ofs) {
        throw std::runtime_error("Failed to open output JSON file.");
    }
    this->dump_json(ofs, indent);
}

inline void FirCfg::load_json_file(const char* file_path) {
    std::ifstream ifs(file_path);
    if (!ifs) {
        throw std::runtime_error("Failed to open input JSON file.");
    }
    this->load_json(ifs);
}

#endif // INCLUDE_FIR_CFG_TB_H