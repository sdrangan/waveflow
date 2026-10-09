/* sched_replay.c -- replay the micro-scheduler's trace on gem5, one measured region per operation.
 *
 * The trace (examples/cpu_sched, MicroScheduler.trace) is one line per scheduler operation:
 *     a <gid> <prio>   add a job
 *     d <gid> 0        remove the head (a dispatch); the hand-off that follows is its own region
 *     r <gid> <prio>   reprio: remove the job, re-insert it at <prio>
 * The list algorithm is the calibrated one (waveflow/cpu/calib/kernels/sched_ops.c, same insertion
 * and removal), so each region measures exactly the operation the model priced.
 *
 * One warm-up pass replays the whole trace outside any region, then the list is emptied and the
 * trace replayed with every operation -- and every dispatch hand-off -- in its own region, back to
 * back: warm caches, as in calibration.  The program prints the counters it saw, which the harness
 * compares with the trace (the Python run's counters) before comparing any cycles.
 *
 * Every measured region is followed by an EMPTY region.  The markers' own cost depends on their
 * state: a calibration program enters its single region with the m5 code cold (94 cycles on HPI at
 * 1.2 GHz, matching empty.c), but back-to-back regions run it hot and far cheaper.  So the replay
 * measures the overhead in context and the harness subtracts each region's following empty one.
 *
 * mode=total instead puts ONE region around the whole replay and no per-operation markers: the
 * marker-free total (per-operation markers perturb what they measure by tens of cycles).
 *
 * Args: trace=<path> [mode=per_op|total]
 */
#include "wf_kernel.h"

typedef struct {
    uint32_t id;
    int32_t prio;
} tg_t;

typedef struct {
    char op;
    uint32_t gid;
    int32_t prio;
} trace_op_t;

static uint64_t n_scanned, n_moved;

static inline int tg_less(tg_t a, tg_t b) {
    return a.prio < b.prio || (a.prio == b.prio && a.id < b.id);
}

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

/* The dispatch hand-off: one call through a function pointer (calibrated as dispatch.c). */
static WF_NOINLINE uint32_t h0(uint32_t x) { return x + 1u; }
static WF_NOINLINE uint32_t h1(uint32_t x) { return x * 3u; }
static WF_NOINLINE uint32_t h2(uint32_t x) { return x ^ 0x5a5au; }
typedef uint32_t (*handler_t)(uint32_t);
static handler_t volatile table[3] = {h0, h1, h2};

/* Apply one operation to L (length *n); returns 1 when it was a dispatch. */
static WF_NOINLINE int apply(tg_t *L, uint32_t *n, trace_op_t t) {
    n_scanned = n_moved = 0;
    if (t.op == 'a') {
        tg_t g = {t.gid, t.prio};
        tg_insert(L, *n, g);
        *n += 1;
        return 0;
    }
    if (t.op == 'd') {
        tg_remove(L, *n, t.gid);
        *n -= 1;
        return 1;
    }
    tg_remove(L, *n, t.gid);
    tg_t g = {t.gid, t.prio};
    tg_insert(L, *n - 1, g);
    return 0;
}

int main(int argc, char **argv) {
    const char *path = wf_arg_str(argc, argv, "trace", "trace.txt");
    const int per_op = strcmp(wf_arg_str(argc, argv, "mode", "per_op"), "total") != 0;
    FILE *f = fopen(path, "r");
    if (!f) {
        fprintf(stderr, "cannot open %s\n", path);
        return 2;
    }
    uint32_t cap = 1024, n_ops = 0;
    trace_op_t *ops = malloc(sizeof(trace_op_t) * cap);
    char op;
    unsigned gid;
    int prio;
    while (fscanf(f, " %c %u %d", &op, &gid, &prio) == 3) {
        if (n_ops == cap) ops = realloc(ops, sizeof(trace_op_t) * (cap *= 2));
        ops[n_ops].op = op;
        ops[n_ops].gid = gid;
        ops[n_ops].prio = prio;
        n_ops++;
    }
    fclose(f);

    tg_t *L = malloc(sizeof(tg_t) * (n_ops + 1));
    uint64_t *scanned = malloc(sizeof(uint64_t) * n_ops), *moved = malloc(sizeof(uint64_t) * n_ops);
    uint32_t *tasks = malloc(sizeof(uint32_t) * n_ops);
    uint32_t n = 0, acc = 0, n_dispatch = 0;

    for (uint32_t i = 0; i < n_ops; ++i) /* warm-up pass, outside any region */
        if (apply(L, &n, ops[i])) acc = table[ops[i].gid % 3u](acc);
    n = 0;
    acc = 0;

    if (!per_op) WF_ROI_BEGIN(); /* mode=total: one region around everything */
    for (uint32_t i = 0; i < n_ops; ++i) {
        tasks[i] = n;
        if (per_op) WF_ROI_BEGIN();
        int dispatched = apply(L, &n, ops[i]);
        if (per_op) {
            WF_ROI_END();
            WF_ROI_BEGIN(); /* the markers' cost in this context */
            WF_ROI_END();
        }
        scanned[i] = n_scanned;
        moved[i] = n_moved;
        if (dispatched) {
            if (per_op) WF_ROI_BEGIN();
            acc = table[ops[i].gid % 3u](acc);
            if (per_op) {
                WF_ROI_END();
                WF_ROI_BEGIN();
                WF_ROI_END();
            }
            n_dispatch++;
        }
    }
    if (!per_op) WF_ROI_END();

    wf_json_begin("sched_replay");
    wf_json_u64("n_ops", n_ops);
    wf_json_u64("n_dispatch", n_dispatch);
    wf_json_u64("final_len", n);
    wf_json_u64("acc", acc);
    printf(", \"n_tasks\": [");
    for (uint32_t i = 0; i < n_ops; ++i) printf(i ? ", %u" : "%u", tasks[i]);
    printf("], \"n_scanned\": [");
    for (uint32_t i = 0; i < n_ops; ++i) printf(i ? ", %llu" : "%llu", (unsigned long long)scanned[i]);
    printf("], \"n_moved\": [");
    for (uint32_t i = 0; i < n_ops; ++i) printf(i ? ", %llu" : "%llu", (unsigned long long)moved[i]);
    printf("]");
    wf_json_end();
    return 0;
}
