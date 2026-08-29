#ifndef STREAMUTILS_TB_H
#define STREAMUTILS_TB_H

#include <ap_int.h>
#include <cstdint>
#include <cctype>
#include <cstdlib>
#include <fstream>
#include <hls_stream.h>
#if __has_include(<hls_axi_stream.h>)
#include <hls_axi_stream.h>
#else
#include <ap_axi_sdata.h>
#endif
#include "streamutils_hls.h"
#include <stdexcept>
#include <string>
#include <vector>

namespace streamutils {

    inline const char* to_string(tlast_status s) {
        int idx = static_cast<int>(s);
        if (idx < 0 || idx >= tlast_status_info::count) {
            return "unknown";
        }
        return tlast_status_info::names[idx];
    }

    inline uint32_t read_le_uint32(std::istream& is) {
        uint32_t value = 0;
        for (int i = 0; i < 4; ++i) {
            const int byte = is.get();
            if (byte == std::char_traits<char>::eof()) {
                throw std::runtime_error("Unexpected end of uint32 binary file.");
            }
            value |= static_cast<uint32_t>(static_cast<unsigned char>(byte)) << (8 * i);
        }
        return value;
    }

    inline void write_le_uint32(std::ostream& os, uint32_t value) {
        for (int i = 0; i < 4; ++i) {
            os.put(static_cast<char>((value >> (8 * i)) & 0xFFu));
            if (!os) {
                throw std::runtime_error("Failed to write uint32 binary file.");
            }
        }
    }

    template<typename T>
    void read_uint32_file(T& value, const char* file_path) {
        std::ifstream ifs(file_path, std::ios::binary);
        if (!ifs) {
            throw std::runtime_error(std::string("Failed to open input file: ") + file_path);
        }

        std::vector<ap_uint<32>> words;
        while (ifs.peek() != std::ifstream::traits_type::eof()) {
            words.push_back(read_le_uint32(ifs));
        }

        if (words.empty()) {
            throw std::runtime_error(std::string("No uint32 words found in input file: ") + file_path);
        }

        value.template read_array<32>(words.data());
    }

    template<typename T>
    void read_uint32_file_len(T& value, const char* file_path, int n0) {
        std::ifstream ifs(file_path, std::ios::binary);
        if (!ifs) {
            throw std::runtime_error(std::string("Failed to open input file: ") + file_path);
        }

        const int nwords = T::template nwords_len<32>(n0);
        std::vector<ap_uint<32>> words;
        words.reserve(nwords);
        for (int i = 0; i < nwords; ++i) {
            words.push_back(read_le_uint32(ifs));
        }

        if (ifs.peek() != std::ifstream::traits_type::eof()) {
            throw std::runtime_error(std::string("Unexpected trailing bytes in input file: ") + file_path);
        }

        value.template read_array<32>(words.data(), n0);
    }

    template<typename T>
    void write_uint32_file(const T& value, const char* file_path) {
        std::ofstream ofs(file_path, std::ios::binary);
        if (!ofs) {
            throw std::runtime_error(std::string("Failed to open output file: ") + file_path);
        }

        std::vector<ap_uint<32>> words(T::template nwords<32>());
        value.template write_array<32>(words.data());
        for (const auto& word : words) {
            write_le_uint32(ofs, static_cast<uint32_t>(word));
        }
    }

    template<typename T>
    void write_uint32_file_len(const T& value, const char* file_path, int n0) {
        std::ofstream ofs(file_path, std::ios::binary);
        if (!ofs) {
            throw std::runtime_error(std::string("Failed to open output file: ") + file_path);
        }

        std::vector<ap_uint<32>> words(T::template nwords_len<32>(n0));
        value.template write_array<32>(words.data(), n0);
        for (const auto& word : words) {
            write_le_uint32(ofs, static_cast<uint32_t>(word));
        }
    }

    inline void json_skip_ws(const std::string& s, size_t& pos) {
        while (pos < s.size() && std::isspace(static_cast<unsigned char>(s[pos]))) {
            ++pos;
        }
    }

    inline void json_expect_char(const std::string& s, size_t& pos, char ch) {
        json_skip_ws(s, pos);
        if (pos >= s.size() || s[pos] != ch) {
            throw std::runtime_error("Malformed JSON: unexpected delimiter.");
        }
        ++pos;
    }

    inline std::string json_parse_string(const std::string& s, size_t& pos) {
        json_skip_ws(s, pos);
        if (pos >= s.size() || s[pos] != '"') {
            throw std::runtime_error("Malformed JSON: expected string key.");
        }
        ++pos;

        std::string out;
        while (pos < s.size()) {
            char c = s[pos++];
            if (c == '"') {
                return out;
            }
            if (c == '\\') {
                if (pos >= s.size()) {
                    throw std::runtime_error("Malformed JSON: invalid escape sequence.");
                }
                char esc = s[pos++];
                switch (esc) {
                case '"': out.push_back('"'); break;
                case '\\': out.push_back('\\'); break;
                case '/': out.push_back('/'); break;
                case 'b': out.push_back('\b'); break;
                case 'f': out.push_back('\f'); break;
                case 'n': out.push_back('\n'); break;
                case 'r': out.push_back('\r'); break;
                case 't': out.push_back('\t'); break;
                default:
                    throw std::runtime_error("Malformed JSON: unsupported escape sequence.");
                }
            } else {
                out.push_back(c);
            }
        }
        throw std::runtime_error("Malformed JSON: unterminated string.");
    }

    inline double json_parse_number(const std::string& s, size_t& pos) {
        json_skip_ws(s, pos);
        if (pos >= s.size()) {
            throw std::runtime_error("Malformed JSON: expected number.");
        }

        const char* begin = s.c_str() + pos;
        char* end = nullptr;
        double value = std::strtod(begin, &end);
        if (end == begin) {
            throw std::runtime_error("Malformed JSON: invalid numeric value.");
        }
        pos += static_cast<size_t>(end - begin);
        return value;
    }

} // namespace streamutils

#endif // STREAMUTILS_TB_H