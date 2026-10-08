/* dispatch.c -- dequeue-and-call: the scheduler's dispatch overhead per task.
 *
 * n entries of a queue name one of three handlers (drawn by the PRNG); each is dequeued and called
 * through a function pointer, threading an accumulator.  The handlers are trivial on purpose: the
 * cost measured is the dequeue, the indirect call and the return.
 *
 * Counters: n_dispatch.   Twin: kernels/numeric.py (dispatch).   Args: n=<calls> seed=<u32>
 */
#include "wf_kernel.h"

static WF_NOINLINE uint32_t h0(uint32_t x) { return x + 1u; }
static WF_NOINLINE uint32_t h1(uint32_t x) { return x * 3u; }
static WF_NOINLINE uint32_t h2(uint32_t x) { return x ^ 0x5a5au; }

typedef uint32_t (*handler_t)(uint32_t);
static handler_t volatile table[3] = {h0, h1, h2};

static WF_NOINLINE uint32_t dispatch(const uint8_t *queue, uint32_t n) {
    uint32_t acc = 0;
    for (uint32_t i = 0; i < n; ++i) acc = table[queue[i]](acc);
    return acc;
}

int main(int argc, char **argv) {
    uint32_t n = (uint32_t)wf_arg_u64(argc, argv, "n", 64);
    wf_seed((uint32_t)wf_arg_u64(argc, argv, "seed", 1));
    uint8_t *queue = malloc((size_t)n + 1);
    for (uint32_t i = 0; i < n; ++i) queue[i] = (uint8_t)(wf_rand() % 3u);
    uint32_t acc = dispatch(queue, n); /* warm-up */
    WF_ROI_BEGIN();
    acc = dispatch(queue, n);
    WF_ROI_END();
    wf_json_begin("dispatch");
    wf_json_u64("n_dispatch", n);
    wf_json_u64("acc", acc);
    wf_json_end();
    free(queue);
    return 0;
}
