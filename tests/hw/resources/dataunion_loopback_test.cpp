__EXTRA_INCLUDES__
#include "__HEADER__"

#include <fstream>
#include <iostream>
#include <vector>

int main(int argc, char** argv) {
    const char* in_path  = (argc > 1) ? argv[1] : "dataunion_words_in.txt";
    const char* out_path = (argc > 2) ? argv[2] : "dataunion_words_out.txt";

    static constexpr int NWORDS   = __NWORDS__;
    static constexpr int NPACKETS = __NPACKETS__;
    static constexpr int WORD_BW  = __WORD_BW__;

    std::ifstream fin(in_path);
    if (!fin) {
        std::cerr << "Cannot open input file: " << in_path << std::endl;
        return 1;
    }

    std::vector<ap_uint<WORD_BW>> words_all;
    unsigned long long v;
    while (fin >> v) words_all.push_back((ap_uint<WORD_BW>)v);

    if ((int)words_all.size() != NWORDS * NPACKETS) {
        std::cerr << "Expected " << (NWORDS * NPACKETS)
                  << " words, got " << words_all.size() << std::endl;
        return 1;
    }

    std::ofstream fout(out_path);
    if (!fout) {
        std::cerr << "Cannot open output file: " << out_path << std::endl;
        return 1;
    }

    for (int p = 0; p < NPACKETS; p++) {
        ap_uint<WORD_BW> words_in[NWORDS];
        for (int i = 0; i < NWORDS; i++) words_in[i] = words_all[p * NWORDS + i];

        // Every generated form, chained: array in, plain stream, AXI4-Stream (TLAST on the last
        // word), array out -- each reader and writer must agree with the Python layout.
        __DATAUNION_CLASS__ du;
        du.read_array<WORD_BW>(words_in);

        hls::stream<ap_uint<WORD_BW> > s;
        du.write_stream<WORD_BW>(s);
        if ((int)s.size() != NWORDS) {
            std::cerr << "write_stream wrote " << s.size() << " words, expected " << NWORDS << std::endl;
            return 2;
        }
        __DATAUNION_CLASS__ du2;
        du2.read_stream<WORD_BW>(s);

        hls::stream<streamutils::axi4s_word<WORD_BW> > a;
        du2.write_axi4_stream<WORD_BW>(a);
        if ((int)a.size() != NWORDS) {
            std::cerr << "write_axi4_stream wrote " << a.size() << " words, expected " << NWORDS << std::endl;
            return 3;
        }
        __DATAUNION_CLASS__ du3;
        streamutils::tlast_status tl;
        du3.read_axi4_stream<WORD_BW>(a, tl);
        if (tl != streamutils::tlast_status::tlast_at_end) {
            std::cerr << "read_axi4_stream: TLAST not at the end of packet " << p << std::endl;
            return 4;
        }

        ap_uint<WORD_BW> words_out[NWORDS];
        du3.write_array<WORD_BW>(words_out);

        for (int i = 0; i < NWORDS; i++) {
            fout << (unsigned long long)((ap_uint<64>)words_out[i]) << "\n";
        }
    }

    return 0;
}
