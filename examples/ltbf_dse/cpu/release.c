/* release.c -- the scheduler's successor bookkeeping (plans/ltbf_dse/ltbf_dse_spec.md §5.6): when a
 * job finishes, decrement each successor's count of unfinished dependencies and collect those that
 * reach zero.  The twin is examples/ltbf_dse/hw/sched.py's release(); its counters n_succ and
 * n_ready are the cost model's features.
 *
 * Inputs: `succ` successors, a permutation of 0..succ-1 drawn from the harness PRNG (wf_kernel.h);
 * the first `ready` ids have one dependency left, the others two, so exactly `ready` become ready.
 *
 *   ./release succ=8 ready=3 seed=1
 */
#include "wf_kernel.h"

#define SMAX 4096

static uint32_t succ_ids[SMAX], ready_ids[SMAX];
static int32_t pending[SMAX];

static WF_NOINLINE uint32_t release(const uint32_t *succ, uint32_t n, int32_t *pend, uint32_t *ready) {
    uint32_t nr = 0;
    for (uint32_t k = 0; k < n; ++k) {
        uint32_t s = succ[k];
        pend[s] -= 1;
        if (pend[s] == 0) ready[nr++] = s;
    }
    return nr;
}

static void reset(uint32_t n, uint32_t n_ready) {
    for (uint32_t i = 0; i < n; ++i) pending[i] = i < n_ready ? 1 : 2;
}

int main(int argc, char **argv) {
    uint32_t n = (uint32_t)wf_arg_u64(argc, argv, "succ", 8);
    uint32_t n_ready = (uint32_t)wf_arg_u64(argc, argv, "ready", 1);
    wf_seed((uint32_t)wf_arg_u64(argc, argv, "seed", 1));
    if (n < 1 || n > SMAX || n_ready > n) return 2;
    for (uint32_t i = 0; i < n; ++i) succ_ids[i] = i;
    for (uint32_t i = n - 1; i > 0; --i) { /* Fisher-Yates */
        uint32_t j = wf_rand() % (i + 1);
        uint32_t t = succ_ids[i];
        succ_ids[i] = succ_ids[j];
        succ_ids[j] = t;
    }
    reset(n, n_ready);
    uint32_t nr = release(succ_ids, n, pending, ready_ids); /* warm-up */
    reset(n, n_ready);
    WF_ROI_BEGIN();
    nr = release(succ_ids, n, pending, ready_ids);
    WF_ROI_END();
    uint64_t h = 0;
    for (uint32_t k = 0; k < nr; ++k) h = (h * 31u + ready_ids[k]) & 0xffffffffu;
    wf_json_begin("release");
    wf_json_u64("n_succ", (uint64_t)n);
    wf_json_u64("n_ready", (uint64_t)nr);
    wf_json_u64("checksum", h);
    wf_json_end();
    return 0;
}
