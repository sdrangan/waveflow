/* swapcontext.c -- the same ping-pong as ctx_switch.c, through libc's swapcontext.  INFORMATIONAL.
 *
 * swapcontext saves the signal mask with rt_sigprocmask, a syscall; gem5's syscall-emulation mode
 * emulates it at near-zero cost, so this number under-counts what a kernel entry costs on silicon.
 * It is recorded beside ctx_switch for comparison and never used as the platform's switch cost.
 *
 * Counters: n_switches.   Twin: kernels/numeric.py (swapcontext).   Args: k=<round trips>
 */
#include "wf_kernel.h"

#include <ucontext.h>

static volatile uint64_t count;
static ucontext_t main_ctx, co_ctx;
static char co_stack[1 << 16];

static void co_entry(void) {
    for (;;) {
        count++;
        swapcontext(&co_ctx, &main_ctx);
    }
}

static WF_NOINLINE void pingpong(uint64_t k) {
    for (uint64_t i = 0; i < k; ++i) swapcontext(&main_ctx, &co_ctx);
}

int main(int argc, char **argv) {
    uint64_t k = wf_arg_u64(argc, argv, "k", 64);
    getcontext(&co_ctx);
    co_ctx.uc_stack.ss_sp = co_stack;
    co_ctx.uc_stack.ss_size = sizeof co_stack;
    co_ctx.uc_link = NULL;
    makecontext(&co_ctx, co_entry, 0);
    pingpong(k); /* warm-up */
    count = 0;
    WF_ROI_BEGIN();
    pingpong(k);
    WF_ROI_END();
    wf_json_begin("swapcontext");
    wf_json_u64("n_switches", 2 * k);
    wf_json_u64("count", count);
    wf_json_end();
    return 0;
}
