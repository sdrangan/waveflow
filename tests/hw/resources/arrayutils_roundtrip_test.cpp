#include <fstream>
#include <iostream>
#include <vector>

#include "__HEADER__"

int main(int argc, char** argv) {
    const char* in_words_path = (argc > 1) ? argv[1] : "array_words.txt";
    const char* out_words_path = (argc > 2) ? argv[2] : "array_words_out.txt";

    std::ifstream in_words(in_words_path);
    if (!in_words) {
        std::cerr << "Failed to open input words file: " << in_words_path << std::endl;
        return 1;
    }

    std::vector<ap_uint<__WORD_BW__>> words;
    unsigned long long raw = 0;
    while (in_words >> raw) {
        words.push_back((ap_uint<__WORD_BW__>)raw);
    }

    if (words.empty()) {
        std::cerr << "No serialized words found." << std::endl;
        return 1;
    }

    __NAMESPACE__::value_type data[__ARRAY_LEN__];
    // Whole-array overload (range [0, N)); N is deduced from the statically-sized buffer.
    __NAMESPACE__::read_array_slice<__WORD_BW__>(words.data(), data);

    ap_uint<__WORD_BW__> out_words[__NWORDS__];
    __NAMESPACE__::write_array_slice<__WORD_BW__>(data, out_words);

    std::ofstream out(out_words_path);
    if (!out) {
        std::cerr << "Failed to open output words file: " << out_words_path << std::endl;
        return 1;
    }

    for (int i = 0; i < __NWORDS__; ++i) {
        out << static_cast<unsigned long long>(out_words[i]) << "\n";
    }

    return 0;
}