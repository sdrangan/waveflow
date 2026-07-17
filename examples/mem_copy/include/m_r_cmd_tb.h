#ifndef INCLUDE_M_R_CMD_TB_H
#define INCLUDE_M_R_CMD_TB_H

#include <cctype>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>
#include "streamutils_tb.h"

#include "u_int32_array_tb.h"

#define WAVEFLOW_ENABLE_M_R_CMD_TB_H_MEMBERS
#include "m_r_cmd.h"
#undef WAVEFLOW_ENABLE_M_R_CMD_TB_H_MEMBERS

inline void MRCmd::dump_json(std::ostream& os, int indent, int level) const {
    const int step = (indent < 0) ? 0 : indent;
    os << "{";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"addr\": ";
    os << static_cast<unsigned long long>(this->addr);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"len\": ";
    os << static_cast<unsigned long long>(this->len);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"xfer_len\": ";
    os << static_cast<unsigned long long>(this->xfer_len);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"xfer_msg\": ";
    os << "[";
    for (int i0 = 0; i0 < 8; ++i0) {
    if (i0 > 0) { os << ","; }
    os << static_cast<unsigned long long>(this->xfer_msg.data[i0]);
    }
    os << "]";
    os << "\n";
    for (int i = 0; i < (level) * step; ++i) { os << ' '; }
    os << "}";
}

inline void MRCmd::load_json(const std::string& json_text, size_t& pos) {
    streamutils::json_expect_char(json_text, pos, '{');
    bool seen_root_addr = false;
    bool seen_root_len = false;
    bool seen_root_xfer_len = false;
    bool seen_root_xfer_msg = false;
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
    if (key == "addr") {
        seen_root_addr = true;
        this->addr = static_cast<ap_uint<32>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else if (key == "len") {
        seen_root_len = true;
        this->len = static_cast<ap_uint<32>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else if (key == "xfer_len") {
        seen_root_xfer_len = true;
        this->xfer_len = static_cast<ap_uint<32>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else if (key == "xfer_msg") {
        seen_root_xfer_msg = true;
        streamutils::json_expect_char(json_text, pos, '[');
        for (int i0 = 0; i0 < 8; ++i0) {
            if (i0 > 0) {
                streamutils::json_expect_char(json_text, pos, ',');
            }
            this->xfer_msg.data[i0] = static_cast<ap_uint<32>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
        }
        streamutils::json_expect_char(json_text, pos, ']');
    }
    else {
        throw std::runtime_error("Malformed JSON: unexpected key for schema.");
    }
    }
    if (!seen_root_addr) {
    throw std::runtime_error("Malformed JSON: missing required key 'addr'.");
    }
    if (!seen_root_len) {
    throw std::runtime_error("Malformed JSON: missing required key 'len'.");
    }
    if (!seen_root_xfer_len) {
    throw std::runtime_error("Malformed JSON: missing required key 'xfer_len'.");
    }
    if (!seen_root_xfer_msg) {
    throw std::runtime_error("Malformed JSON: missing required key 'xfer_msg'.");
    }
}

inline void MRCmd::load_json(std::istream& is) {
    std::string json_text((std::istreambuf_iterator<char>(is)), std::istreambuf_iterator<char>());
    size_t pos = 0;
    streamutils::json_skip_ws(json_text, pos);
    this->load_json(json_text, pos);
    streamutils::json_skip_ws(json_text, pos);
    if (pos != json_text.size()) {
        throw std::runtime_error("Trailing characters after JSON object.");
    }
}

inline void MRCmd::dump_json_file(const char* file_path, int indent) const {
    std::ofstream ofs(file_path);
    if (!ofs) {
        throw std::runtime_error("Failed to open output JSON file.");
    }
    this->dump_json(ofs, indent);
}

inline void MRCmd::load_json_file(const char* file_path) {
    std::ifstream ifs(file_path);
    if (!ifs) {
        throw std::runtime_error("Failed to open input JSON file.");
    }
    this->load_json(ifs);
}

#endif // INCLUDE_M_R_CMD_TB_H