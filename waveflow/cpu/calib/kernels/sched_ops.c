/* sched_ops.c -- a micro-scheduler's task-group operations, the branchy control code of the suite.
 *
 * A ready list of task groups is kept sorted by (prio, id), lower first.  One operation is measured:
 *   add     insert a new group (scan from the tail, shifting larger ones up)
 *   delete  remove a group by id (scan from the head, shift the rest down)
 *   reprio  change a group's priority (remove, then insert as add does)
 *   sort    insertion-sort an unsorted list of n groups
 * The semantics follow tracerspecsense's task-group operations (tg_add / tg_del / tg_pri_change /
 * tg_sort) re-implemented small; nothing of it is copied.
 *
 * Counters: n_tasks (list length before the op), n_scanned (comparisons), n_moved (element moves).
 * Twin: kernels/sched_ops.py.   Args: op=add|delete|reprio|sort n=<groups> seed=<u32>
 */
#include "wf_kernel.h"

#define PRIO_LEVELS 64u

typedef struct {
    uint32_t id;
    int32_t prio;
} tg_t;

static uint64_t n_scanned, n_moved;

static inline int tg_less(tg_t a, tg_t b) {
    return a.prio < b.prio || (a.prio == b.prio && a.id < b.id);
}

/* Insert g into the sorted list L[0..n), which has room for n+1. */
static WF_NOINLINE void tg_insert(tg_t *L, uint32_t n, tg_t g) {
    uint32_t j = n;
    while (j > 0) {
        n_scanned++;
        if (tg_less(g, L[j - 1])) {
            L[j] = L[j - 1];
            n_moved++;
            j--;
        } else {
            break;
        }
    }
    L[j] = g;
}

/* Remove the group with this id from L[0..n); returns its index (n if absent). */
static WF_NOINLINE uint32_t tg_remove(tg_t *L, uint32_t n, uint32_t id) {
    uint32_t i = 0;
    while (i < n) {
        n_scanned++;
        if (L[i].id == id) break;
        i++;
    }
    if (i == n) return n;
    for (uint32_t k = i; k + 1 < n; ++k) {
        L[k] = L[k + 1];
        n_moved++;
    }
    return i;
}

static WF_NOINLINE void tg_sort(tg_t *L, uint32_t n) {
    for (uint32_t i = 1; i < n; ++i) {
        tg_t key = L[i];
        uint32_t j = i;
        while (j > 0) {
            n_scanned++;
            if (tg_less(key, L[j - 1])) {
                L[j] = L[j - 1];
                n_moved++;
                j--;
            } else {
                break;
            }
        }
        L[j] = key;
    }
}

/* The measured operation.  p0/p1 are its pre-drawn parameters. */
static WF_NOINLINE uint32_t run_op(int op, tg_t *L, uint32_t n, uint32_t p0, int32_t p1) {
    n_scanned = n_moved = 0;
    switch (op) {
    case 0: { /* add: p1 is the new group's prio; its id is n */
        tg_t g = {n, p1};
        tg_insert(L, n, g);
        return n + 1;
    }
    case 1: /* delete: p0 is the id */
        tg_remove(L, n, p0);
        return n - 1;
    case 2: { /* reprio: p0 the id, p1 the new prio */
        tg_remove(L, n, p0);
        tg_t g = {p0, p1};
        tg_insert(L, n - 1, g);
        return n;
    }
    default: /* sort */
        tg_sort(L, n);
        return n;
    }
}

int main(int argc, char **argv) {
    const char *ops = wf_arg_str(argc, argv, "op", "add");
    int op = strcmp(ops, "add") == 0      ? 0
             : strcmp(ops, "delete") == 0 ? 1
             : strcmp(ops, "reprio") == 0 ? 2
             : strcmp(ops, "sort") == 0   ? 3
                                          : -1;
    uint32_t n = (uint32_t)wf_arg_u64(argc, argv, "n", 16);
    wf_seed((uint32_t)wf_arg_u64(argc, argv, "seed", 1));
    if (op < 0 || (op != 0 && n == 0)) {
        fprintf(stderr, "bad op or n\n");
        return 2;
    }

    tg_t *init = malloc(sizeof(tg_t) * (n + 1));
    tg_t *work = malloc(sizeof(tg_t) * (n + 1));
    for (uint32_t i = 0; i < n; ++i) {
        init[i].id = i;
        init[i].prio = (int32_t)(wf_rand() % PRIO_LEVELS);
    }
    if (op != 3) { /* every op but sort starts from a sorted list (not measured, not counted) */
        uint64_t s = n_scanned, m = n_moved;
        tg_sort(init, n);
        n_scanned = s;
        n_moved = m;
    }
    uint32_t p0 = 0;
    int32_t p1 = 0;
    if (op == 0) p1 = (int32_t)(wf_rand() % PRIO_LEVELS);
    if (op == 1 || op == 2) p0 = wf_rand() % n;
    if (op == 2) p1 = (int32_t)(wf_rand() % PRIO_LEVELS);

    memcpy(work, init, sizeof(tg_t) * n); /* warm-up on a copy */
    run_op(op, work, n, p0, p1);
    memcpy(work, init, sizeof(tg_t) * n);
    WF_ROI_BEGIN();
    uint32_t n_after = run_op(op, work, n, p0, p1);
    WF_ROI_END();

    uint32_t h = 0;
    for (uint32_t i = 0; i < n_after; ++i) h = h * 31u + work[i].id * 65599u + (uint32_t)work[i].prio;

    wf_json_begin("sched_ops");
    wf_json_str("op", ops);
    wf_json_u64("n_tasks", n);
    wf_json_u64("n_scanned", n_scanned);
    wf_json_u64("n_moved", n_moved);
    wf_json_u64("checksum", h);
    wf_json_end();
    free(init);
    free(work);
    return 0;
}
