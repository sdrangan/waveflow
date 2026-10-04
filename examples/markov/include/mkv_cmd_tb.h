#ifndef INCLUDE_MKV_CMD_TB_H
#define INCLUDE_MKV_CMD_TB_H

#include <cctype>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>
#include "streamutils_tb.h"

#define WAVEFLOW_ENABLE_MKV_CMD_TB_H_MEMBERS
#include "mkv_cmd.h"
#undef WAVEFLOW_ENABLE_MKV_CMD_TB_H_MEMBERS

inline void MkvCmd::dump_json(std::ostream& os, int indent, int level) const {
    const int step = (indent < 0) ? 0 : indent;
    os << "{";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"n\": ";
    os << static_cast<unsigned long long>(this->n);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"tx_id\": ";
    os << static_cast<unsigned long long>(this->tx_id);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"x0\": ";
    os << static_cast<unsigned long long>(this->x0);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"seed\": ";
    os << static_cast<unsigned long long>(this->seed);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"p01\": ";
    os << static_cast<unsigned long long>(this->p01);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"p10\": ";
    os << static_cast<unsigned long long>(this->p10);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"dstaddr\": ";
    os << static_cast<unsigned long long>(this->dstaddr);
    os << "\n";
    for (int i = 0; i < (level) * step; ++i) { os << ' '; }
    os << "}";
}

inline void MkvCmd::load_json(const std::string& json_text, size_t& pos) {
    streamutils::json_expect_char(json_text, pos, '{');
    bool seen_root_n = false;
    bool seen_root_tx_id = false;
    bool seen_root_x0 = false;
    bool seen_root_seed = false;
    bool seen_root_p01 = false;
    bool seen_root_p10 = false;
    bool seen_root_dstaddr = false;
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
    else if (key == "tx_id") {
        seen_root_tx_id = true;
        this->tx_id = static_cast<ap_uint<16>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else if (key == "x0") {
        seen_root_x0 = true;
        this->x0 = static_cast<ap_uint<16>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else if (key == "seed") {
        seen_root_seed = true;
        this->seed = static_cast<ap_uint<32>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else if (key == "p01") {
        seen_root_p01 = true;
        this->p01 = static_cast<ap_uint<16>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else if (key == "p10") {
        seen_root_p10 = true;
        this->p10 = static_cast<ap_uint<16>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else if (key == "dstaddr") {
        seen_root_dstaddr = true;
        this->dstaddr = static_cast<ap_uint<64>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else {
        throw std::runtime_error("Malformed JSON: unexpected key for schema.");
    }
    }
    if (!seen_root_n) {
    throw std::runtime_error("Malformed JSON: missing required key 'n'.");
    }
    if (!seen_root_tx_id) {
    throw std::runtime_error("Malformed JSON: missing required key 'tx_id'.");
    }
    if (!seen_root_x0) {
    throw std::runtime_error("Malformed JSON: missing required key 'x0'.");
    }
    if (!seen_root_seed) {
    throw std::runtime_error("Malformed JSON: missing required key 'seed'.");
    }
    if (!seen_root_p01) {
    throw std::runtime_error("Malformed JSON: missing required key 'p01'.");
    }
    if (!seen_root_p10) {
    throw std::runtime_error("Malformed JSON: missing required key 'p10'.");
    }
    if (!seen_root_dstaddr) {
    throw std::runtime_error("Malformed JSON: missing required key 'dstaddr'.");
    }
}

inline void MkvCmd::load_json(std::istream& is) {
    std::string json_text((std::istreambuf_iterator<char>(is)), std::istreambuf_iterator<char>());
    size_t pos = 0;
    streamutils::json_skip_ws(json_text, pos);
    this->load_json(json_text, pos);
    streamutils::json_skip_ws(json_text, pos);
    if (pos != json_text.size()) {
        throw std::runtime_error("Trailing characters after JSON object.");
    }
}

inline void MkvCmd::dump_json_file(const char* file_path, int indent) const {
    std::ofstream ofs(file_path);
    if (!ofs) {
        throw std::runtime_error("Failed to open output JSON file.");
    }
    this->dump_json(ofs, indent);
}

inline void MkvCmd::load_json_file(const char* file_path) {
    std::ifstream ifs(file_path);
    if (!ifs) {
        throw std::runtime_error("Failed to open input JSON file.");
    }
    this->load_json(ifs);
}

#endif // INCLUDE_MKV_CMD_TB_H