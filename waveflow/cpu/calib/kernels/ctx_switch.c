/* ctx_switch.c -- the cost of a cooperative context switch, the scheduler's per-task overhead.
 *
 * A main context and one coroutine ping-pong k times: 2k switches.  On aarch64 the switch is
 * hand-written: it saves and restores the AAPCS64 callee-saved state (x19-x30, d8-d15) and the stack
 * pointer, with NO syscall -- the switch a bare-metal or RTOS scheduler does.  That is the primary
 * measurement: swapcontext (swapcontext.c) is measured only for information, because under gem5's
 * syscall-emulation mode its rt_sigprocmask call is emulated at near-zero cost.
 *
 * On any other host (the twin test runs on x86) the same ping-pong uses ucontext, so the program's
 * output can still be checked; its timing there means nothing and is never used.
 *
 * Counters: n_switches.   Twin: kernels/numeric.py (ctx_switch).   Args: k=<round trips>
 */
#include "wf_kernel.h"

static volatile uint64_t count;
static uint64_t k_iter;

#if defined(__aarch64__)

/* void wf_switch(uint64_t *save_sp, uint64_t new_sp): push callee-saved state, save sp to
 * *save_sp, load new_sp, pop that context's state, return into it.  A 160-byte frame:
 * x19..x30 (96 bytes) then d8..d15 (64 bytes); x30 (the return address) sits at offset 88. */
__asm__(".text\n"
        ".global wf_switch\n"
        ".type wf_switch, %function\n"
        "wf_switch:\n"
        "  sub sp, sp, #160\n"
        "  stp x19, x20, [sp, #0]\n"
        "  stp x21, x22, [sp, #16]\n"
        "  stp x23, x24, [sp, #32]\n"
        "  stp x25, x26, [sp, #48]\n"
        "  stp x27, x28, [sp, #64]\n"
        "  stp x29, x30, [sp, #80]\n"
        "  stp d8, d9, [sp, #96]\n"
        "  stp d10, d11, [sp, #112]\n"
        "  stp d12, d13, [sp, #128]\n"
        "  stp d14, d15, [sp, #144]\n"
        "  mov x9, sp\n"
        "  str x9, [x0]\n"
        "  mov sp, x1\n"
        "  ldp x19, x20, [sp, #0]\n"
        "  ldp x21, x22, [sp, #16]\n"
        "  ldp x23, x24, [sp, #32]\n"
        "  ldp x25, x26, [sp, #48]\n"
        "  ldp x27, x28, [sp, #64]\n"
        "  ldp x29, x30, [sp, #80]\n"
        "  ldp d8, d9, [sp, #96]\n"
        "  ldp d10, d11, [sp, #112]\n"
        "  ldp d12, d13, [sp, #128]\n"
        "  ldp d14, d15, [sp, #144]\n"
        "  add sp, sp, #160\n"
        "  ret\n"
        ".size wf_switch, .-wf_switch\n");
void wf_switch(uint64_t *save_sp, uint64_t new_sp);

#define STACK_WORDS 4096
static uint64_t co_stack[STACK_WORDS] __attribute__((aligned(16)));
static uint64_t main_sp, co_sp;

static void __attribute__((noreturn)) co_entry(void) {
    for (;;) {
        count++;
        wf_switch(&co_sp, main_sp);
    }
}

static void co_init(void) {
    uint64_t *frame = &co_stack[STACK_WORDS - 20]; /* 160 bytes below the top */
    memset(frame, 0, 160);
    frame[11] = (uint64_t)(uintptr_t)co_entry; /* x30: where the first switch "returns" */
    co_sp = (uint64_t)(uintptr_t)frame;
}

static WF_NOINLINE void pingpong(uint64_t k) {
    for (uint64_t i = 0; i < k; ++i) wf_switch(&main_sp, co_sp);
}

#else /* host fallback: ucontext, for the twin test only */

#include <ucontext.h>
static ucontext_t main_ctx, co_ctx;
static char co_stack[1 << 16];

static void co_entry(void) {
    for (;;) {
        count++;
        swapcontext(&co_ctx, &main_ctx);
    }
}

static void co_init(void) {
    getcontext(&co_ctx);
    co_ctx.uc_stack.ss_sp = co_stack;
    co_ctx.uc_stack.ss_size = sizeof co_stack;
    co_ctx.uc_link = NULL;
    makecontext(&co_ctx, co_entry, 0);
}

static WF_NOINLINE void pingpong(uint64_t k) {
    for (uint64_t i = 0; i < k; ++i) swapcontext(&main_ctx, &co_ctx);
}

#endif

int main(int argc, char **argv) {
    k_iter = wf_arg_u64(argc, argv, "k", 64);
    co_init();
    pingpong(k_iter); /* warm-up: also enters the coroutine for the first time */
    count = 0;
    WF_ROI_BEGIN();
    pingpong(k_iter);
    WF_ROI_END();
    wf_json_begin("ctx_switch");
    wf_json_u64("n_switches", 2 * k_iter);
    wf_json_u64("count", count);
    wf_json_end();
    return 0;
}
