#ifndef INCLUDE_COPY_CMD_TB_H
#define INCLUDE_COPY_CMD_TB_H

#include <cctype>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>
#include "streamutils_tb.h"

#define WAVEFLOW_ENABLE_COPY_CMD_TB_H_MEMBERS
#include "copy_cmd.h"
#undef WAVEFLOW_ENABLE_COPY_CMD_TB_H_MEMBERS

inline void CopyCmd::dump_json(std::ostream& os, int indent, int level) const {
    const int step = (indent < 0) ? 0 : indent;
    os << "{";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"src_off\": ";
    os << static_cast<unsigned long long>(this->src_off);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"dst_off\": ";
    os << static_cast<unsigned long long>(this->dst_off);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"n_words\": ";
    os << static_cast<unsigned long long>(this->n_words);
    os << "\n";
    for (int i = 0; i < (level) * step; ++i) { os << ' '; }
    os << "}";
}

inline void CopyCmd::load_json(const std::string& json_text, size_t& pos) {
    streamutils::json_expect_char(json_text, pos, '{');
    bool seen_root_src_off = false;
    bool seen_root_dst_off = false;
    bool seen_root_n_words = false;
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
    if (key == "src_off") {
        seen_root_src_off = true;
        this->src_off = static_cast<ap_uint<32>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else if (key == "dst_off") {
        seen_root_dst_off = true;
        this->dst_off = static_cast<ap_uint<32>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else if (key == "n_words") {
        seen_root_n_words = true;
        this->n_words = static_cast<ap_uint<32>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else {
        throw std::runtime_error("Malformed JSON: unexpected key for schema.");
    }
    }
    if (!seen_root_src_off) {
    throw std::runtime_error("Malformed JSON: missing required key 'src_off'.");
    }
    if (!seen_root_dst_off) {
    throw std::runtime_error("Malformed JSON: missing required key 'dst_off'.");
    }
    if (!seen_root_n_words) {
    throw std::runtime_error("Malformed JSON: missing required key 'n_words'.");
    }
}

inline void CopyCmd::load_json(std::istream& is) {
    std::string json_text((std::istreambuf_iterator<char>(is)), std::istreambuf_iterator<char>());
    size_t pos = 0;
    streamutils::json_skip_ws(json_text, pos);
    this->load_json(json_text, pos);
    streamutils::json_skip_ws(json_text, pos);
    if (pos != json_text.size()) {
        throw std::runtime_error("Trailing characters after JSON object.");
    }
}

inline void CopyCmd::dump_json_file(const char* file_path, int indent) const {
    std::ofstream ofs(file_path);
    if (!ofs) {
        throw std::runtime_error("Failed to open output JSON file.");
    }
    this->dump_json(ofs, indent);
}

inline void CopyCmd::load_json_file(const char* file_path) {
    std::ifstream ifs(file_path);
    if (!ifs) {
        throw std::runtime_error("Failed to open input JSON file.");
    }
    this->load_json(ifs);
}

#endif // INCLUDE_COPY_CMD_TB_H