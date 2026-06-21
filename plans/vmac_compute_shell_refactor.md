# Task: make `VmacAccel.vmac_compute` the memory-owning synthesizable shell; `execute` the pure golden

You are working in the Waveflow repo (`c:\Users\sdran\Documents\repos\pysilicon`,
package `waveflow`, local folder/venv still named `pysilicon`). This is a focused,
behavior-preserving refactor of the VMAC example. Read this whole brief before editing.

## Background / why

VMAC has a sim/synthesis split. In the **generated C++** kernel
(`examples/vmac/vmac_compute_impl.tpp`), `vmac_compute(cmd, mem*)` takes the `m_axi`
pointer and does **all its own memory access internally** — pipelined `read_array_lane`
reads, `ab_eq` B-alias suppression, reduce accumulators, `write_array_lane` for Y. There
is **no pre-loaded flat buffer** in the kernel.

In the **Python sim**, by contrast, `run_proc` pre-loads a flat packed buffer (`local`),
then calls `execute(cmd, local)` which addresses operands inside that buffer via
`_region_idx`. That `local` buffer is a pure simulation crutch — it doesn't exist in
hardware, it precludes modeling pipelined reads cleanly, and it makes `execute` impure
(it does addressing + writeback, not just math).

**Goal:** make the sim structurally match the kernel.
- `execute` becomes the **one pure math golden**: operand arrays in, dst out. No memory,
  no addressing, no writeback, no timing.
- `vmac_compute` becomes the **synthesizable shell**: it owns memory access (via the
  already-added `MMIFMaster.read_array_pipelined` / `write_array_pipelined`) and timing
  capture, and delegates the math to `execute`. Its Python body is sim-only (not extracted
  for codegen); its C++ remains the hand-written `.tpp`.
- `run_proc` becomes a thin queue consumer that calls `vmac_compute`.
- Delete `local` and the flat-buffer addressing.

The two legitimate implementations stay: `execute` (the Python golden) and the `.tpp`
(the hand-written synthesizable C++ twin, conformance-checked bit-exact). Nothing else
should duplicate the math.

## Decisions already made (do not re-litigate)

1. `execute`'s signature changes from `(cmd, mem)` to `(cmd, a, b=None, alpha=None)`
   taking dense operand arrays. Update the numeric/golden tests accordingly (this removes
   their flat-mem `build()` helper — a simplification).
2. The sim shell reads each region as **one contiguous block** (`row_stride == n_cols`,
   the current assumption) and reshapes to `(n, m)`. Arbitrary `row_stride` stays a sim
   limitation, as today. Do not try to model strided sub-matrices.
3. Transaction recording (`_record_txn`) moves into `vmac_compute` (it follows the
   reads/writes). The II-decoupling pad also lives at the `vmac_compute`/command level —
   keep it computed so the **total command latency is unchanged** (see "must stay
   identical" below).

## Critical constraint: preserve the synthesizable signature

**Do NOT reorder `vmac_compute`'s parameters.** Keep it `vmac_compute(self, cmd, mem)`.
The `@synthesizable(impl_file="vmac_compute_impl.tpp")` codegen maps this signature to the
C++ call `vmac_compute(cmd, mem)`; reordering would break codegen. The trick: in the
**sim**, `mem` is now the **memory interface** (`self.m_mem`, an `MMIFMaster`) instead of
the flat `local` buffer. In C++, `mem` is the `m_axi` pointer. Same arg, same position,
aligned semantics ("the memory the kernel reads/writes"). `run_proc` calls
`yield from self.vmac_compute(cmd, self.m_mem)`.

The `@synthesizable(impl_file=...)` body is **not** extracted for codegen, so you may
freely rewrite the sim body (pipelined reads + `execute` + writes + timing). The generated
C++ call is unchanged because the signature is unchanged.

## Current code map (read these first)

- `examples/vmac/vmac.py`
  - `execute(self, cmd, mem)` — the golden; uses `_region_idx`, `_operand`, `_alpha`,
    `_writeback`; writes into `mem` and returns dst.
  - `vmac_compute(self, cmd, mem)` — `@synthesizable(impl_file=...)`; body is
    `return self.execute(cmd, mem)` + unreachable `yield`.
  - `run_proc(self)` — dequeues `VmacCmd`; builds `local`; calls `_read_region` for
    A / B (unless `ab_eq`) / indirect alpha; `dst = self.execute(cmd, local)`; writes Y via
    `write_array_pipelined`; applies the II-pad; records cmd timing.
  - Helpers: `_byte_addr`, `_read_region`, `_record_txn`, `_region_idx`, `_operand`,
    `_alpha`, `_writeback`, `_data_elem`, `_in_fmt`, `pf`, geometry cached as
    `self._mem_bw` / `self._elem` / `self._elem_words` / `self._elem_bytes` in `run_proc`.
  - `MMIFMaster.read_array_pipelined(element_type, count, addr, word_bw=...)` →
    `(data, tstart, tend, nwords)`; `write_array_pipelined(elements, element_type, addr,
    word_bw=...)` → `(tstart, tend, nwords)`. Already implemented in `waveflow/hw/memif.py`.
- `examples/vmac/vmac_compute_impl.tpp` — the C++ kernel (read-only reference; do NOT edit;
  confirms the kernel owns memory access).
- `tests/examples/test_vmac_golden.py`, `tests/examples/test_vmac_numeric.py` — call
  `accel.execute(...)`; must be updated to the new signature.
- `examples/vmac/vmac_build.py` — **also calls `execute(cmd, mem)` and relies on its
  flat-memory writeback** to generate the Vitis conformance/cosim expected vectors:
  `golden_case` (~line 321, `dst = accel.execute(cmd, post)` then reads `post` as the
  expected memory image) and `_tput_vectors` (~line 652). This is NOT synthesizable codegen
  — it's the golden-vector generator that runs even in the non-Vitis `py_sim`/`gen` steps.
  It MUST be updated to the new `execute` signature (see "Update vmac_build.py" below), or
  `python vmac_build.py --through py_sim` breaks. Note: VMAC's synthesizable C++ does NOT
  come from `run_proc` — the top + TB are hand-rendered f-strings in this file
  (`render_top` / `render_tb`) calling the `.tpp` hook; only the schema / array-utils
  headers are auto-generated. So the codegen path is unaffected by this refactor; only the
  Python golden-vector generation needs the signature update.

## Target shapes

```python
# --- the ONE golden: pure math, no memory / addressing / writeback / timing ---
def execute(self, cmd: VmacCmd, a, b=None, alpha=None) -> DataArray:
    in_fmt = self._in_fmt()
    n, m = int(cmd.n_rows), int(cmd.n_cols)
    out_cls = self.output_format(cmd)
    A = self._operand(np.asarray(a).reshape(n, m), in_fmt)
    op = OpCode(int(cmd.op))
    if op is OpCode.scalar_mult:
        t = cmult(self._alpha(cmd.alpha, n, m, in_fmt, alpha), A)   # alpha array or direct imm
    elif op is OpCode.inner_prod:
        t = cmult(A, conj(self._operand(np.asarray(b).reshape(n, m), in_fmt)))
    else:  # sum
        t = cadd(A, self._operand(np.asarray(b).reshape(n, m), in_fmt))
    if bool(cmd.reduce):
        t = csum(t, axis=0)
    # keep the existing accumulator-format assertion block
    return self._requantize(t, out_cls)        # NOTE: no _writeback

# --- the synthesizable shell: owns memory + timing, delegates math to the golden ---
@synthesizable(impl_file="vmac_compute_impl.tpp")
def vmac_compute(self, cmd: VmacCmd, mem) -> ProcessGen[DataArray]:
    # `mem` is the MMIFMaster in sim (the m_axi pointer in C++). Reads/writes are pipelined.
    n, m = int(cmd.n_rows), int(cmd.n_cols)
    nm = n * m
    op = OpCode(int(cmd.op))
    need_b = op in (OpCode.inner_prod, OpCode.sum)
    ab_eq = need_b and int(cmd.b.addr) == int(cmd.a.addr)
    alpha_indirect = op is OpCode.scalar_mult and not bool(cmd.alpha.direct)
    cmd_idx, ab = self._cmd_idx, ab_eq      # however cmd_idx is threaded (see note)

    a, t0, t1, nw = yield from mem.read_array_pipelined(
        self._elem, nm, self._byte_addr(int(cmd.a.addr)), word_bw=self._mem_bw)
    self._record_txn(cmd_idx, self.region_labels.get(int(cmd.a.addr), "data"),
                     "read", self._byte_addr(int(cmd.a.addr)), nw, t0, t1, ab)
    b = None
    if need_b and not ab_eq:
        b, t0, t1, nw = yield from mem.read_array_pipelined(
            self._elem, nm, self._byte_addr(int(cmd.b.addr)), word_bw=self._mem_bw)
        self._record_txn(cmd_idx, self.region_labels.get(int(cmd.b.addr), "data"),
                         "read", self._byte_addr(int(cmd.b.addr)), nw, t0, t1, ab)
    elif ab_eq:
        b = a
    alpha = None
    if alpha_indirect:
        alpha, t0, t1, nw = yield from mem.read_array_pipelined(
            self._elem, n, self._byte_addr(int(cmd.alpha.addr)), word_bw=self._mem_bw)
        self._record_txn(...)  # same pattern

    dst = self.execute(cmd, a, b, alpha)

    y_addr = self._byte_addr(int(cmd.y.addr))
    t0, t1, nw = yield from mem.write_array_pipelined(
        dst, type(dst).element_type, y_addr, word_bw=self._mem_bw)
    self._record_txn(cmd_idx, self.region_labels.get(int(cmd.y.addr), "Y"),
                     "write", y_addr, nw, t0, t1, ab)
    return dst

# --- run_proc: thin queue consumer ---
# while True:
#   cmd = yield from self.cmd_queue.get(self.Cmd, poll_interval=poll)
#   cmd_idx += 1; dequeue_t = self.now; record dequeue (q_events + logger)
#   if OpCode(int(cmd.op)) is OpCode.end: break
#   dst = yield from self.vmac_compute(cmd, self.m_mem)
#   # II-pad to the calibrated schedule (keep EXACT current computation):
#   if self.timing is not None:
#       trips = n * math.ceil(m / self.pf)
#       sched_secs = self.timing.cycles(trips) / float(self.clk.freq)
#       pad = sched_secs - (self.now - dequeue_t)
#       if pad > 0: yield self.timeout(pad)
#   complete_t = self.now; append cmd_records (cmd_idx, op, ab_eq, n_rows, n_cols,
#                                              dequeue_t, complete_t, latency)
```

Notes / freedom:
- `cmd_idx` threading: pass `cmd_idx` (and `ab_eq` if convenient) into `vmac_compute`, or
  stash on `self` before the call — your choice; keep it clean. The `_record_txn` calls
  need `cmd_idx` and `ab_eq`.
- Keep the II-pad in `run_proc` (it needs `dequeue_t`); `vmac_compute` only does the
  reads/compute/write/txn-records. This satisfies decision 3 (records move into
  `vmac_compute`) while keeping latency math byte-identical. (If you prefer the pad inside
  `vmac_compute`, you must thread `dequeue_t` in and produce identical numbers — but the
  simpler split above is recommended.)
- Delete only `local` and `_read_region` once nothing references them. **KEEP `_region_idx`
  and `_writeback`** — `execute` no longer calls `_writeback` internally (it returns `dst`,
  the sim shell does the real write via `write_array_pipelined`), but `vmac_build.py`'s
  vector generation still needs `_writeback`/`_region_idx` to form the expected flat-memory
  image (see "Update vmac_build.py"). Keep `_byte_addr`, `_operand`, `_alpha` (modify
  `_alpha` to take the pre-read alpha array instead of reading `mem`), `_record_txn`, and
  the geometry caching.
- `_alpha(sc, n_rows, n_cols, in_fmt, alpha_arr)`: direct → build from `sc.imm` (as today);
  indirect → broadcast the passed `alpha_arr` (shape `(n_rows,)`) across columns. It must no
  longer read from a `mem` array.
- The geometry (`self._mem_bw`, `self._elem`, `self._elem_words`, `self._elem_bytes`) is
  currently cached at the top of `run_proc`; keep that (both `run_proc` and `vmac_compute`
  use it). `vmac_compute` runs only via `run_proc`, so the cache is set before it's called.

## Update the tests

`tests/examples/test_vmac_golden.py` and `test_vmac_numeric.py` currently build a flat
`mem` and call `accel.execute(cmd, mem)`. Change them to call
`accel.execute(cmd, a, b, alpha)` with the dense operand arrays they already have (drop the
flat-mem `build()`/layout plumbing where it was only feeding `execute`). The command still
needs valid `a/b/y/alpha` region fields for any code that reads them, but `execute` no
longer indexes a flat buffer. Keep the oracle/expected comparisons identical. If a test
also exercises the full sim path (queue), leave that path intact.

## Update vmac_build.py (REQUIRED — conformance vector generation)

`golden_case` and `_tput_vectors` currently call `accel.execute(cmd, post)` where `post` is
a flat memory image they then read back as the *expected* result the Vitis kernel must
reproduce. With `execute` now pure, they must (a) call `execute` with operand arrays and
(b) write `dst` into the flat `post` image themselves:

```python
# golden_case — it already has the operand pairs (a, b, alpha) for the case:
in_fmt = accel._in_fmt()
A = cx.make_complex(a[0], a[1], in_fmt)                 # (n, m)
B = cx.make_complex(b[0], b[1], in_fmt) if need_b else None
ALPHA = cx.make_complex(alpha[0], alpha[1], in_fmt) if (op is OpCode.scalar_mult
                                                        and np.ndim(alpha[0]) > 0) else None
dst = accel.execute(cmd, A, B, ALPHA)
post = mem.copy()
accel._writeback(post, cmd.y, dst)                     # form the expected flat image
# ... then mem_exp_words = _mem_words(post, in_elem, cfg.mem_dwidth) as before
```

`_tput_vectors` needs the same treatment (it has `a`, `b` and a direct scalar alpha). The
expected flat-memory words (`mem_exp_words` / the cosim `mem_exp.txt`) MUST be byte-identical
to before — that's what keeps the Vitis conformance bit-exact. Verify with `--through py_sim`
(oracle parity, non-Vitis) and, if you run Vitis, `--through csim`.

## Verification (run all of these; everything must pass)

Use the project venv explicitly (a fresh shell otherwise defaults to system Python 3.14
with no deps, so "0 failed" can mean nothing ran):

```
../pysilicon-venv/Scripts/python.exe -m pytest tests/examples/test_vmac_golden.py tests/examples/test_vmac_numeric.py -q
../pysilicon-venv/Scripts/python.exe -m pytest tests/hw tests/examples tests/simulation -m "not vitis" --tb=short -q
../pysilicon-venv/Scripts/python.exe -m examples.vmac.vmac_queue_sim
../pysilicon-venv/Scripts/python.exe examples/vmac/vmac_build.py --through py_sim   # golden-vs-oracle (non-Vitis); must pass
../pysilicon-venv/Scripts/python.exe -m ruff check examples/vmac/vmac.py examples/vmac/vmac_build.py tests/examples/test_vmac_golden.py tests/examples/test_vmac_numeric.py
```

**Baseline (pre-existing, NOT your regressions):** the non-vitis suite has known failures
unrelated to this work — `tests/hw/test_dataschema_poly.py` (missing
`examples/stream_inband/poly.hpp`) and some `test_build` / poly-timing tests. A branch is
clean iff its failures are a **subset** of main's. Confirm by diffing against a fresh `main`
run if unsure; do not "fix" these.

**Must stay identical** — the queue sim is behavior-preserving. The run must still print:
- `rho matches numpy reference: OK`
- `read-bus words : anorm(ab_eq)=16  abcorr=32  (anorm = half of abcorr: True)`
- `latency : anorm=3463.9 ns  abcorr=3463.9 ns  gap=0.0 ns`
- `sim drained at t = 10083.9 ns`
- `read accounting: 3 read blocks, 48 words; sum(durations)=1190.0 ns`
- `OK - metrics hold; timeline emitted.`

If any of these drift, your reads/writes or the II-pad reference changed behavior — fix it
before proceeding (most likely cause: read granularity or the `dequeue_t` reference for the
pad).

**Optional (only if Vitis time is acceptable):** Vitis HLS 2025.1 is installed here, so the
cosim genuinely runs. The synthesizable signature is unchanged and the `.tpp` is untouched,
so codegen should be unaffected — but if you want belt-and-suspenders, run a single VMAC
cosim/csynth test (`-m vitis -k vmac`) and confirm it still passes. Do not block the task on
this if it's slow; note in your summary whether you ran it.

## Definition of done

- `execute(cmd, a, b, alpha)` is pure (no memory/addressing/writeback); `vmac_compute(cmd,
  mem)` owns the pipelined reads/writes + txn records and calls `execute`; `run_proc` calls
  `vmac_compute` and keeps the II-pad + cmd bookkeeping.
- `local` and `_read_region` deleted; `_region_idx` / `_writeback` retained (vmac_build
  vector gen); no dead imports (ruff clean).
- `vmac_build.py` updated: `golden_case` / `_tput_vectors` use the new `execute` signature
  and write `dst` into the flat image; `--through py_sim` passes; expected vectors unchanged.
- All verification commands pass; queue-sim headline metrics identical to the values above.
- The numeric/golden tests pass against the new `execute` signature.

## Wrap-up

- Do this on a new branch `vmac-compute-shell` (branch from `main`). Make 2–3 logically
  scoped commits (e.g. "execute: pure golden + test update", "vmac: vmac_compute owns
  memory; run_proc thin; drop local"). End each commit message with:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- **Do not push and do not merge** — leave the branch for review.
- Finish with a short summary: what changed, the verification results (paste the queue-sim
  headline block), whether you ran the Vitis check, and anything you were unsure about.
