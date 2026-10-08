/* cdot_q15.c -- a complex dot product in Q15 fixed point: compute-bound, streaming, cache-friendly.
 *
 * y = sum_i a[i] * conj(b[i]), with int16 inputs and int64 products and accumulation (no overflow
 * for n < 2^32), then rounded back to Q15:  out = (acc + 2^14) >> 15  (arithmetic shift).
 * Fixed point, not float, so the twin is bit-exact without disabling the compiler's multiply-add
 * fusion (plans/cpu_model.md, review finding 9).
 *
 * Counters: n.   Working set: 8 bytes per sample (four int16).   Twin: kernels/numeric.py (cdot_q15).
 * Args: n=<samples> seed=<u32>
 */
#include "wf_kernel.h"

static WF_NOINLINE void cdot(const int16_t *ar, const int16_t *ai, const int16_t *br,
                             const int16_t *bi, uint32_t n, int64_t *re, int64_t *im) {
    int64_t sr = 0, si = 0;
    for (uint32_t k = 0; k < n; ++k) {
        /* Each product fits int32, but two of them summed can reach 2^31: widen before adding. */
        int64_t xr = ar[k], xi = ai[k], yr = br[k], yi = bi[k];
        sr += xr * yr + xi * yi;
        si += xi * yr - xr * yi;
    }
    *re = (sr + (1 << 14)) >> 15;
    *im = (si + (1 << 14)) >> 15;
}

int main(int argc, char **argv) {
    uint32_t n = (uint32_t)wf_arg_u64(argc, argv, "n", 64);
    wf_seed((uint32_t)wf_arg_u64(argc, argv, "seed", 1));
    int16_t *ar = malloc(2 * (size_t)n + 2), *ai = malloc(2 * (size_t)n + 2);
    int16_t *br = malloc(2 * (size_t)n + 2), *bi = malloc(2 * (size_t)n + 2);
    for (uint32_t k = 0; k < n; ++k) {
        ar[k] = (int16_t)(wf_rand() & 0xffffu);
        ai[k] = (int16_t)(wf_rand() & 0xffffu);
        br[k] = (int16_t)(wf_rand() & 0xffffu);
        bi[k] = (int16_t)(wf_rand() & 0xffffu);
    }
    int64_t re, im;
    cdot(ar, ai, br, bi, n, &re, &im); /* warm-up */
    WF_ROI_BEGIN();
    cdot(ar, ai, br, bi, n, &re, &im);
    WF_ROI_END();

    wf_json_begin("cdot_q15");
    wf_json_u64("n", n);
    wf_json_i64("re", re);
    wf_json_i64("im", im);
    wf_json_end();
    free(ar);
    free(ai);
    free(br);
    free(bi);
    return 0;
}
