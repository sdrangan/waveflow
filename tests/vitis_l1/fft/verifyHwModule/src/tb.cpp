// Testbench for the generated top, shared by csim and cosim.
#include <cstdio>
#include <vector>
#include <ap_int.h>
#include <hls_stream.h>
static const int L = 16, IN_W = 16, OUT_W = 21, R = 4;
void vitis_fft_top(hls::stream<ap_uint<2*IN_W> >&, hls::stream<ap_uint<2*IN_W> >&,
                   hls::stream<ap_uint<2*IN_W> >&, hls::stream<ap_uint<2*IN_W> >&,
                   hls::stream<ap_uint<2*OUT_W> >&, hls::stream<ap_uint<2*OUT_W> >&,
                   hls::stream<ap_uint<2*OUT_W> >&, hls::stream<ap_uint<2*OUT_W> >&);
int main(int argc, char** argv) {
    FILE* f = fopen(argv[1], "r"); int n_vec = 0;
    if (!f) { printf("WF_TB_ERR: cannot open %s\n", argv[1]); return 1; }
    if (fscanf(f, "%d", &n_vec) != 1) return 2;
    std::vector<long long> ir(n_vec*L), ii(n_vec*L);
    for (int k = 0; k < n_vec*L; k++) if (fscanf(f, "%lld %lld", &ir[k], &ii[k]) != 2) return 2;
    fclose(f);
    FILE* o = fopen(argv[2], "w");
    if (!o) { printf("WF_TB_ERR: cannot open %s\n", argv[2]); return 1; }
    for (int v = 0; v < n_vec; v++) {
        hls::stream<ap_uint<2*IN_W> > si[R]; hls::stream<ap_uint<2*OUT_W> > mo[R];
        for (int i = 0; i < L/R; i++) for (int j = 0; j < R; j++) {
            int n = i*R + j; ap_uint<2*IN_W> w = 0;
            w.range(IN_W-1, 0) = (ap_uint<IN_W>)ir[v*L+n];
            w.range(2*IN_W-1, IN_W) = (ap_uint<IN_W>)ii[v*L+n];
            si[j].write(w);
        }
        vitis_fft_top(si[0],si[1],si[2],si[3], mo[0],mo[1],mo[2],mo[3]);
        for (int i = 0; i < L/R; i++) for (int j = 0; j < R; j++) {
            ap_uint<2*OUT_W> w = mo[j].read();
            fprintf(o, "%llu %llu\n", (unsigned long long)w.range(OUT_W-1,0).to_uint64(),
                    (unsigned long long)w.range(2*OUT_W-1,OUT_W).to_uint64());
        }
    }
    fclose(o); printf("WF_TB_OK %d vectors\n", n_vec); return 0;
}
