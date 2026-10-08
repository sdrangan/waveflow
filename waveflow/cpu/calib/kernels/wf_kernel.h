/* wf_kernel.h -- what every calibration kernel shares.
 *
 * Each kernel is a small C program with a bit-exact Python twin (registered in kernels/__init__.py).
 * The two must agree on outputs AND on the work counters the cost model reads, so the program's
 * inputs come from the same PRNG in both (xorshift32, below), and the program prints one JSON line
 * the harness compares with the twin.
 *
 * The measured region:  WF_ROI_BEGIN / WF_ROI_END wrap exactly the call being measured.  Built with
 * -DWF_GEM5 they are gem5's m5ops (reset the statistics, dump them), so the dumped block holds only
 * that call; built for the host they are empty, which is how the twin tests run the same source.
 * Inputs are drawn and a warm-up call is made OUTSIDE the region: measurements are warm-cache.
 *
 * Arguments are key=value pairs:  ./sched_ops op=add n=100 seed=7
 */
#ifndef WF_KERNEL_H
#define WF_KERNEL_H

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#ifdef WF_GEM5
#include <gem5/m5ops.h>
#define WF_ROI_BEGIN() m5_reset_stats(0, 0)
#define WF_ROI_END() m5_dump_stats(0, 0)
#else
#define WF_ROI_BEGIN() ((void)0)
#define WF_ROI_END() ((void)0)
#endif

#define WF_NOINLINE __attribute__((noinline))

/* xorshift32; a zero seed is replaced by 1 (xorshift's fixed point).  Twin: kernels/common.py. */
static uint32_t wf_rng_state = 1u;
static inline void wf_seed(uint32_t s) { wf_rng_state = s ? s : 1u; }
static inline uint32_t wf_rand(void) {
    uint32_t x = wf_rng_state;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    wf_rng_state = x;
    return x;
}

/* key=value lookup over argv; returns dflt when the key is absent. */
static inline const char *wf_arg_str(int argc, char **argv, const char *key, const char *dflt) {
    size_t k = strlen(key);
    for (int i = 1; i < argc; ++i)
        if (strncmp(argv[i], key, k) == 0 && argv[i][k] == '=') return argv[i] + k + 1;
    return dflt;
}
static inline uint64_t wf_arg_u64(int argc, char **argv, const char *key, uint64_t dflt) {
    const char *v = wf_arg_str(argc, argv, key, NULL);
    return v ? strtoull(v, NULL, 10) : dflt;
}

/* One JSON object per run: wf_json_begin, any number of fields, wf_json_end. */
static inline void wf_json_begin(const char *kernel) { printf("{\"kernel\": \"%s\"", kernel); }
static inline void wf_json_u64(const char *key, uint64_t v) {
    printf(", \"%s\": %llu", key, (unsigned long long)v);
}
static inline void wf_json_i64(const char *key, int64_t v) {
    printf(", \"%s\": %lld", key, (long long)v);
}
static inline void wf_json_str(const char *key, const char *v) {
    printf(", \"%s\": \"%s\"", key, v);
}
static inline void wf_json_end(void) { printf("}\n"); }

#endif
