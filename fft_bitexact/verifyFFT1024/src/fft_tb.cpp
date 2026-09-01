// fft_tb.cpp -- testbench shared by C-simulation and C/RTL co-simulation.
//
// Reads data/input.txt, runs every vector through fft_top, writes the results as raw stored
// integers.  Raw bits, never decimals: a float round-trip can absorb exactly the 1-LSB
// differences this whole exercise exists to detect.
//
//   argv[1] = input file
//   argv[2] = output file
//
// The same binary serves both runs; run.tcl passes different output paths so the C-sim and
// co-sim results can be diffed against each other as well as against the Python model.
#include <cstdio>
#include <cstdlib>
#include <vector>
#include "fft_top.hpp"

int main(int argc, char** argv) {
    const char* in_path = (argc > 1) ? argv[1] : "data/input.txt";
    const char* out_path = (argc > 2) ? argv[2] : "results/output.txt";

    FILE* fi = fopen(in_path, "r");
    if (!fi) { printf("WAVEFLOW_ERROR: cannot open input %s\n", in_path); return 1; }

    int n_vec = 0, n_samp = 0, c;
    // skip '#' comment lines
    while ((c = fgetc(fi)) != EOF) {
        if (c == '#') { while ((c = fgetc(fi)) != EOF && c != '\n') {} }
        else if (c != '\n' && c != ' ' && c != '\r') { ungetc(c, fi); break; }
    }
    if (fscanf(fi, "%d %d", &n_vec, &n_samp) != 2) {
        printf("WAVEFLOW_ERROR: bad header in %s\n", in_path); fclose(fi); return 1;
    }
    if (n_samp != FFT_L) {
        printf("WAVEFLOW_ERROR: input has %d samples, DUT is L=%d\n", n_samp, FFT_L);
        fclose(fi); return 1;
    }

    std::vector<long long> in_re(n_vec * n_samp), in_im(n_vec * n_samp);
    for (int k = 0; k < n_vec * n_samp; k++)
        if (fscanf(fi, "%lld %lld", &in_re[k], &in_im[k]) != 2) {
            printf("WAVEFLOW_ERROR: short input file at sample %d\n", k); fclose(fi); return 1;
        }
    fclose(fi);

    FILE* fo = fopen(out_path, "w");
    if (!fo) { printf("WAVEFLOW_ERROR: cannot open output %s\n", out_path); return 1; }
    fprintf(fo, "# Vitis SSR FFT verification output\n");
    fprintf(fo, "# raw STORED integers of ap_fixed<%d,%d>, unsigned two's-complement\n",
            (int)T_out::value_type::width, (int)T_out::value_type::iwidth);
    fprintf(fo, "# out_W out_I n_vectors n_samples\n");
    fprintf(fo, "%d %d %d %d\n", (int)T_out::value_type::width, (int)T_out::value_type::iwidth,
            n_vec, n_samp);

    for (int v = 0; v < n_vec; v++) {
        hls::stream<T_in> din[FFT_R];
        hls::stream<T_out> dout[FFT_R];
        // Stream layout is the library's own convention (L1/tests/hw/1dfft/.../main.cpp):
        // sample n goes to stream (n % R) at time (n / R).
        for (int i = 0; i < FFT_L / FFT_R; i++)
            for (int j = 0; j < FFT_R; j++) {
                int n = i * FFT_R + j;
                T_in_inner re, im;
                re.range() = (long long)in_re[v * n_samp + n];
                im.range() = (long long)in_im[v * n_samp + n];
                din[j].write(T_in(re, im));
            }

        fft_top(din, dout);

        T_out y[FFT_L];
        for (int i = 0; i < FFT_L / FFT_R; i++)
            for (int j = 0; j < FFT_R; j++) y[i * FFT_R + j] = dout[j].read();
        for (int n = 0; n < FFT_L; n++)
            fprintf(fo, "%lld %lld\n", (long long)y[n].real().range().to_int64(),
                    (long long)y[n].imag().range().to_int64());
    }
    fclose(fo);
    printf("WAVEFLOW_OK: wrote %d vectors x %d samples to %s\n", n_vec, n_samp, out_path);
    return 0;
}
