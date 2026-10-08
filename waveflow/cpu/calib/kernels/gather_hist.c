/* gather_hist.c -- an indexed gather into a histogram: memory-bound, the cache-regime kernel.
 *
 * n increments hist[idx[i]]++ into m uint32 bins, with idx drawn uniformly over the bins.  The bins
 * are the randomly accessed working set (4*m bytes), swept across L1, L2 and DRAM by m; the index
 * stream is read once, in order.  Unsigned 32-bit counts (the twin masks to C's wraparound).
 *
 * Counters: n, m.   Working set: 4*m bytes.   Twin: kernels/numeric.py (gather_hist).
 * Args: n=<increments> m=<bins> seed=<u32>
 */
#include "wf_kernel.h"

static WF_NOINLINE void gather(uint32_t *hist, const uint32_t *idx, uint32_t n) {
    for (uint32_t i = 0; i < n; ++i) hist[idx[i]]++;
}

int main(int argc, char **argv) {
    uint32_t n = (uint32_t)wf_arg_u64(argc, argv, "n", 1024);
    uint32_t m = (uint32_t)wf_arg_u64(argc, argv, "m", 256);
    wf_seed((uint32_t)wf_arg_u64(argc, argv, "seed", 1));
    if (m == 0) {
        fprintf(stderr, "m must be > 0\n");
        return 2;
    }
    uint32_t *idx = malloc(4 * (size_t)n + 4);
    uint32_t *hist = malloc(4 * (size_t)m);
    for (uint32_t i = 0; i < n; ++i) idx[i] = wf_rand() % m;

    memset(hist, 0, 4 * (size_t)m);
    gather(hist, idx, n); /* warm-up: touches the bins */
    memset(hist, 0, 4 * (size_t)m);
    WF_ROI_BEGIN();
    gather(hist, idx, n);
    WF_ROI_END();

    uint32_t h = 0;
    for (uint32_t j = 0; j < m; ++j) h = h * 31u + hist[j];
    wf_json_begin("gather_hist");
    wf_json_u64("n", n);
    wf_json_u64("m", m);
    wf_json_u64("checksum", h);
    wf_json_end();
    free(idx);
    free(hist);
    return 0;
}
