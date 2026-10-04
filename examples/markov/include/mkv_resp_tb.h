#ifndef INCLUDE_MKV_RESP_TB_H
#define INCLUDE_MKV_RESP_TB_H

#include <cctype>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>
#include "streamutils_tb.h"

#define WAVEFLOW_ENABLE_MKV_RESP_TB_H_MEMBERS
#include "mkv_resp.h"
#undef WAVEFLOW_ENABLE_MKV_RESP_TB_H_MEMBERS

inline void MkvResp::dump_json(std::ostream& os, int indent, int level) const {
    const int step = (indent < 0) ? 0 : indent;
    os << "{";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"n\": ";
    os << static_cast<unsigned long long>(this->n);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"ones\": ";
    os << static_cast<unsigned long long>(this->ones);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"tx_id\": ";
    os << static_cast<unsigned long long>(this->tx_id);
    os << "\n";
    for (int i = 0; i < (level) * step; ++i) { os << ' '; }
    os << "}";
}

inline void MkvResp::load_json(const std::string& json_text, size_t& pos) {
    streamutils::json_expect_char(json_text, pos, '{');
    bool seen_root_n = false;
    bool seen_root_ones = false;
    bool seen_root_tx_id = false;
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
    if (key == "n") {
        seen_root_n = true;
        this->n = static_cast<ap_uint<32>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else if (key == "ones") {
        seen_root_ones = true;
        this->ones = static_cast<ap_uint<32>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else if (key == "tx_id") {
        seen_root_tx_id = true;
        this->tx_id = static_cast<ap_uint<16>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else {
        throw std::runtime_error("Malformed JSON: unexpected key for schema.");
    }
    }
    if (!seen_root_n) {
    throw std::runtime_error("Malformed JSON: missing required key 'n'.");
    }
    if (!seen_root_ones) {
    throw std::runtime_error("Malformed JSON: missing required key 'ones'.");
    }
    if (!seen_root_tx_id) {
    throw std::runtime_error("Malformed JSON: missing required key 'tx_id'.");
    }
}

inline void MkvResp::load_json(std::istream& is) {
    std::string json_text((std::istreambuf_iterator<char>(is)), std::istreambuf_iterator<char>());
    size_t pos = 0;
    streamutils::json_skip_ws(json_text, pos);
    this->load_json(json_text, pos);
    streamutils::json_skip_ws(json_text, pos);
    if (pos != json_text.size()) {
        throw std::runtime_error("Trailing characters after JSON object.");
    }
}

inline void MkvResp::dump_json_file(const char* file_path, int indent) const {
    std::ofstream ofs(file_path);
    if (!ofs) {
        throw std::runtime_error("Failed to open output JSON file.");
    }
    this->dump_json(ofs, indent);
}

inline void MkvResp::load_json_file(const char* file_path) {
    std::ifstream ifs(file_path);
    if (!ifs) {
        throw std::runtime_error("Failed to open input JSON file.");
    }
    this->load_json(ifs);
}

#endif // INCLUDE_MKV_RESP_TB_H