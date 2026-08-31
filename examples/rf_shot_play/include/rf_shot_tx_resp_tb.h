#ifndef INCLUDE_RF_SHOT_TX_RESP_TB_H
#define INCLUDE_RF_SHOT_TX_RESP_TB_H

#include <cctype>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>
#include "streamutils_tb.h"

#define WAVEFLOW_ENABLE_RF_SHOT_TX_RESP_TB_H_MEMBERS
#include "rf_shot_tx_resp.h"
#undef WAVEFLOW_ENABLE_RF_SHOT_TX_RESP_TB_H_MEMBERS

inline void ShotTxResp::dump_json(std::ostream& os, int indent, int level) const {
    const int step = (indent < 0) ? 0 : indent;
    os << "{";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"tid\": ";
    os << static_cast<unsigned long long>(this->tid);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"status\": ";
    os << static_cast<unsigned long long>(this->status);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"nsamp_loaded\": ";
    os << static_cast<unsigned long long>(this->nsamp_loaded);
    os << "\n";
    for (int i = 0; i < (level) * step; ++i) { os << ' '; }
    os << "}";
}

inline void ShotTxResp::load_json(const std::string& json_text, size_t& pos) {
    streamutils::json_expect_char(json_text, pos, '{');
    bool seen_root_tid = false;
    bool seen_root_status = false;
    bool seen_root_nsamp_loaded = false;
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
    if (key == "tid") {
        seen_root_tid = true;
        this->tid = static_cast<ap_uint<16>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else if (key == "status") {
        seen_root_status = true;
        this->status = static_cast<ap_uint<16>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else if (key == "nsamp_loaded") {
        seen_root_nsamp_loaded = true;
        this->nsamp_loaded = static_cast<ap_uint<16>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else {
        throw std::runtime_error("Malformed JSON: unexpected key for schema.");
    }
    }
    if (!seen_root_tid) {
    throw std::runtime_error("Malformed JSON: missing required key 'tid'.");
    }
    if (!seen_root_status) {
    throw std::runtime_error("Malformed JSON: missing required key 'status'.");
    }
    if (!seen_root_nsamp_loaded) {
    throw std::runtime_error("Malformed JSON: missing required key 'nsamp_loaded'.");
    }
}

inline void ShotTxResp::load_json(std::istream& is) {
    std::string json_text((std::istreambuf_iterator<char>(is)), std::istreambuf_iterator<char>());
    size_t pos = 0;
    streamutils::json_skip_ws(json_text, pos);
    this->load_json(json_text, pos);
    streamutils::json_skip_ws(json_text, pos);
    if (pos != json_text.size()) {
        throw std::runtime_error("Trailing characters after JSON object.");
    }
}

inline void ShotTxResp::dump_json_file(const char* file_path, int indent) const {
    std::ofstream ofs(file_path);
    if (!ofs) {
        throw std::runtime_error("Failed to open output JSON file.");
    }
    this->dump_json(ofs, indent);
}

inline void ShotTxResp::load_json_file(const char* file_path) {
    std::ifstream ifs(file_path);
    if (!ifs) {
        throw std::runtime_error("Failed to open input JSON file.");
    }
    this->load_json(ifs);
}

#endif // INCLUDE_RF_SHOT_TX_RESP_TB_H