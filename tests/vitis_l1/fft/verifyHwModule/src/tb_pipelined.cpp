#include <cstdio>
#include <vector>
#include <ap_int.h>
#include <hls_stream.h>
static const int L = 16, IN_W = 16, OUT_W = 21, R = 4, NFRAME = 4;
void vitis_fft_top(hls::stream<ap_uint<2*IN_W> >&, hls::stream<ap_uint<2*IN_W> >&,
                   hls::stream<ap_uint<2*IN_W> >&, hls::stream<ap_uint<2*IN_W> >&,
                   hls::stream<ap_uint<2*OUT_W> >&, hls::stream<ap_uint<2*OUT_W> >&,
                   hls::stream<ap_uint<2*OUT_W> >&, hls::stream<ap_uint<2*OUT_W> >&);
int main(int argc, char** argv) {
    FILE* f = fopen(argv[1], "r"); if (!f) { printf("WF_TB_ERR open\n"); return 1; }
    int n_vec = 0; if (fscanf(f, "%d", &n_vec) != 1) return 2;
    std::vector<long long> ir(n_vec*L), ii(n_vec*L);
    for (int k = 0; k < n_vec*L; k++) if (fscanf(f, "%lld %lld", &ir[k], &ii[k]) != 2) return 2;
    fclose(f);
    FILE* o = fopen(argv[2], "w"); if (!o) { printf("WF_TB_ERR out\n"); return 1; }
    hls::stream<ap_uint<2*IN_W> > si[R]; hls::stream<ap_uint<2*OUT_W> > mo[R];
    // 1. all input up front
    for (int v = 0; v < NFRAME; v++)
        for (int i = 0; i < L/R; i++) for (int j = 0; j < R; j++) {
            int n = i*R + j; ap_uint<2*IN_W> w = 0;
            w.range(IN_W-1, 0) = (ap_uint<IN_W>)ir[v*L+n];
            w.range(2*IN_W-1, IN_W) = (ap_uint<IN_W>)ii[v*L+n];
            si[j].write(w);
        }
    // 2. back-to-back calls, nothing drained in between
    for (int v = 0; v < NFRAME; v++)
        vitis_fft_top(si[0],si[1],si[2],si[3], mo[0],mo[1],mo[2],mo[3]);
    // 3. drain
    for (int v = 0; v < NFRAME; v++)
        for (int i = 0; i < L/R; i++) for (int j = 0; j < R; j++) {
            ap_uint<2*OUT_W> w = mo[j].read();
            fprintf(o, "%llu %llu\n", (unsigned long long)w.range(OUT_W-1,0).to_uint64(),
                    (unsigned long long)w.range(2*OUT_W-1,OUT_W).to_uint64());
        }
    fclose(o); printf("WF_TB_OK %d frames pipelined\n", NFRAME); return 0;
}
