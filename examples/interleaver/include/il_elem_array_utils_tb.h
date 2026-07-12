#ifndef INCLUDE_IL_ELEM_ARRAY_UTILS_TB_H
#define INCLUDE_IL_ELEM_ARRAY_UTILS_TB_H

#include "streamutils_tb.h"
#include "il_elem_array_utils.h"

namespace il_elem_array_utils {

/**
 * @brief Read a packed uint32 binary file into an array of value_type.
 *
 * The input file must contain little-endian uint32 words produced by the
 * Python arrayutils.write_uint32_file helper.
 *
 * @param dst Pointer to the destination array.
 * @param file_path Path to the input binary file.
 * @param n0 Number of array elements to decode.
 */
inline void read_uint32_file_array(value_type* dst, const char* file_path, int n0) {
    if (n0 < 0) {
        throw std::runtime_error("n0 must be non-negative.");
    }
    std::ifstream ifs(file_path, std::ios::binary);
    if (!ifs) {
        throw std::runtime_error(std::string("Failed to open input file: ") + file_path);
    }
    const int nwords = get_nwords<32>(n0);
    std::vector<ap_uint<32>> words;
    words.reserve(nwords);
    for (int i = 0; i < nwords; ++i) {
        words.push_back(streamutils::read_le_uint32(ifs));
    }
    if (ifs.peek() != std::ifstream::traits_type::eof()) {
        throw std::runtime_error(std::string("Unexpected trailing bytes in input file: ") + file_path);
    }
    read_array_slice<32>(words.empty() ? nullptr : words.data(), 0, n0, dst);
}

/**
 * @brief Write an array of value_type to a packed uint32 binary file.
 *
 * The output file matches the little-endian uint32 format consumed by the
 * Python arrayutils.read_uint32_file helper.
 *
 * @param src Pointer to the source array.
 * @param file_path Path to the output binary file.
 * @param n0 Number of array elements to encode.
 */
inline void write_uint32_file_array(const value_type* src, const char* file_path, int n0) {
    if (n0 < 0) {
        throw std::runtime_error("n0 must be non-negative.");
    }
    std::ofstream ofs(file_path, std::ios::binary);
    if (!ofs) {
        throw std::runtime_error(std::string("Failed to open output file: ") + file_path);
    }
    const int nwords = get_nwords<32>(n0);
    std::vector<ap_uint<32>> words(nwords);
    write_array_slice<32>(src, words.empty() ? nullptr : words.data(), 0, n0);
    for (const auto& word : words) {
        streamutils::write_le_uint32(ofs, static_cast<uint32_t>(word));
    }
}

}  // namespace il_elem_array_utils

#endif // INCLUDE_IL_ELEM_ARRAY_UTILS_TB_H
