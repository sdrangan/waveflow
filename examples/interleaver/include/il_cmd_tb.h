#ifndef INCLUDE_IL_CMD_TB_H
#define INCLUDE_IL_CMD_TB_H

#include <cctype>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <iterator>
#include <stdexcept>
#include <string>
#include "streamutils_tb.h"

#define WAVEFLOW_ENABLE_IL_CMD_TB_H_MEMBERS
#include "il_cmd.h"
#undef WAVEFLOW_ENABLE_IL_CMD_TB_H_MEMBERS

inline void InterleaverCmd::dump_json(std::ostream& os, int indent, int level) const {
    const int step = (indent < 0) ? 0 : indent;
    os << "{";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"p_off\": ";
    os << static_cast<unsigned long long>(this->p_off);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"x_off\": ";
    os << static_cast<unsigned long long>(this->x_off);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"y_off\": ";
    os << static_cast<unsigned long long>(this->y_off);
    os << ",";
    os << "\n";
    for (int i = 0; i < (level + 1) * step; ++i) { os << ' '; }
    os << "\"n\": ";
    os << static_cast<unsigned long long>(this->n);
    os << "\n";
    for (int i = 0; i < (level) * step; ++i) { os << ' '; }
    os << "}";
}

inline void InterleaverCmd::load_json(const std::string& json_text, size_t& pos) {
    streamutils::json_expect_char(json_text, pos, '{');
    bool seen_root_p_off = false;
    bool seen_root_x_off = false;
    bool seen_root_y_off = false;
    bool seen_root_n = false;
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
    if (key == "p_off") {
        seen_root_p_off = true;
        this->p_off = static_cast<ap_uint<32>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else if (key == "x_off") {
        seen_root_x_off = true;
        this->x_off = static_cast<ap_uint<32>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else if (key == "y_off") {
        seen_root_y_off = true;
        this->y_off = static_cast<ap_uint<32>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else if (key == "n") {
        seen_root_n = true;
        this->n = static_cast<ap_uint<32>>(static_cast<unsigned long long>(streamutils::json_parse_number(json_text, pos)));
    }
    else {
        throw std::runtime_error("Malformed JSON: unexpected key for schema.");
    }
    }
    if (!seen_root_p_off) {
    throw std::runtime_error("Malformed JSON: missing required key 'p_off'.");
    }
    if (!seen_root_x_off) {
    throw std::runtime_error("Malformed JSON: missing required key 'x_off'.");
    }
    if (!seen_root_y_off) {
    throw std::runtime_error("Malformed JSON: missing required key 'y_off'.");
    }
    if (!seen_root_n) {
    throw std::runtime_error("Malformed JSON: missing required key 'n'.");
    }
}

inline void InterleaverCmd::load_json(std::istream& is) {
    std::string json_text((std::istreambuf_iterator<char>(is)), std::istreambuf_iterator<char>());
    size_t pos = 0;
    streamutils::json_skip_ws(json_text, pos);
    this->load_json(json_text, pos);
    streamutils::json_skip_ws(json_text, pos);
    if (pos != json_text.size()) {
        throw std::runtime_error("Trailing characters after JSON object.");
    }
}

inline void InterleaverCmd::dump_json_file(const char* file_path, int indent) const {
    std::ofstream ofs(file_path);
    if (!ofs) {
        throw std::runtime_error("Failed to open output JSON file.");
    }
    this->dump_json(ofs, indent);
}

inline void InterleaverCmd::load_json_file(const char* file_path) {
    std::ifstream ifs(file_path);
    if (!ifs) {
        throw std::runtime_error("Failed to open input JSON file.");
    }
    this->load_json(ifs);
}

#endif // INCLUDE_IL_CMD_TB_H