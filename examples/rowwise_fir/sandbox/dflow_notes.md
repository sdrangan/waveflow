# Control-driven DATAFLOW FIR — Phase-1 de-risk findings

Phase 1 of [`plans/fir-cleanup.md`](../../../plans/fir-cleanup.md) §1: validate, in a
**standalone hand-written HLS sandbox** (no Waveflow framework, no codegen), the
control-driven DATAFLOW kernel shape Phase 2 will *generate*. The shape is the **poly
control shell** (`examples/stream_inband/gen/poly.cpp`) wrapping a hand-written
`fir_dataflow(...)` whose body is a **scoped `#pragma HLS DATAFLOW` region** (load /
compute / store + internal `hls::stream`s), with the error leaving the region through a
**depth-1 status stream**, not a shared scalar.

Files: `fir_dflow_sandbox.{hpp,cpp}` (kernel), `fir_dflow_tb.cpp` (testbench),
`run_dflow.tcl` (Vitis driver), `run_dflow.py` (orchestrator + result capture). All
additive under `sandbox/`; nothing else in `examples/rowwise_fir/` was touched.

Environment: Vitis HLS 2025.1, `xc7z020clg484-1`, 10 ns clock, driven via
`waveflow.toolchain.run_vitis_hls_result(run_dflow.tcl)`. Element type is plain `float`
and gmem is a plain `float*` — the `ap_uint<mem_dwidth>` serialization + built-in
`read_axi4_stream` command deserialize are **§2 (Phase-2) concerns**, deliberately out of
scope here (this sandbox validates the *control + dataflow structure*, with a bit-exact
float golden).

---

## Finding 1 — poly shell + scoped DATAFLOW body synthesizes & cosims bit-exact

**Confirmed.** The top `fir(...)` is the poly shell verbatim in structure:

```cpp
void fir(hls::stream<ap_uint<32>>& s_in, hls::stream<ap_uint<32>>& m_out,
         ap_uint<1>& halted, ap_uint<8>& error, ap_uint<16>& tx_id, float* gmem) {
  // axis s_in/m_out; s_axilite halted/error/tx_id/return + gmem offset; m_axi gmem
  while (true) {
    FirCmdHdr cmd; cmd.read_stream(s_in);          // header read in the TOP (like poly)
    if (cmd.op == FIR_OP_END) return;              // graceful END -> return
    ap_uint<8> err = fir_dataflow(cmd, gmem, m_out);
    if (err) { error = err; tx_id = cmd.tx_id; halted = 1; return; }   // halt + return
  }
}
```

- **Control interface confirmed** identical to poly: `axis` `s_in`/`m_out`,
  `s_axilite bundle=control` for `halted` / `error` / `tx_id` / `return`, plus `m_axi
  bundle=gmem` (`offset=slave`) with its own `s_axilite` offset reg. csynth accepts the
  interface and the `ap_start`-gated `while(1)` command loop.
- **csim bit-exact** for every scenario (`single`, `two`, `three`, `clean`, `error`):
  `WAVEFLOW_FIR_DFLOW_OK`, gmem `Y` matches the C++ golden bit-for-bit (same left-to-right
  tap order — no float tolerance).
- **cosim bit-exact** on the `clean` scenario (3 matrices of varying size 4×64, 2×48,
  3×32 + END): RTL co-simulation **PASS** (`cosim clean: bit-exact PASS`). The poly shell's
  `while(1)`/END-return drives correctly on real RTL.

### DATAFLOW-canonical-form gotchas (the ones that matter for Phase 2)

1. **`err` leaves the region via a depth-1 stream read _after_ the scoped block** — this is
   the canonical "dataflow produces a result the caller consumes" form and it synthesizes:
   ```cpp
   ap_uint<8> fir_dataflow(const FirCmdHdr& cmd, float* gmem, hls::stream<ap_uint<32>>& m_out) {
     hls::stream<ap_uint<8>> err_s;
   #pragma HLS STREAM variable=err_s depth=1     // declared OUTSIDE the region
     {
   #pragma HLS DATAFLOW
       hls::stream<float> ld2cp, taps_s, cp2st;  // inter-task channels INSIDE the region
       hls::stream<FirMeta> ld_meta, cp_meta;
       load(cmd, gmem, ld2cp, taps_s, ld_meta);
       compute(ld2cp, taps_s, ld_meta, cp2st, cp_meta);
       store(cp2st, cp_meta, gmem, m_out, err_s);   // store writes EXACTLY ONE err
     }
     return err_s.read();                         // read AFTER the region (sequential)
   }
   ```
   `err_s` **must be declared in the function scope, outside the `{ #pragma HLS DATAFLOW }`
   block**, so it survives for the post-region read; the inter-task channels are declared
   *inside* the block (canonical). The single `err_s.read()` after the block is what serializes
   "region done → return err".
2. **The header read sits in the top, outside `fir_dataflow`** — so `END`/error → `return`
   stay at the top level (the loop control is not inside a dataflow process). `fir_dataflow`
   receives `cmd` by `const&` (read-only scalar), exactly the Phase-2 contract.
3. **Per-command metadata travels on its own stream** (`FirMeta`: effective `n_rows`,
   `n_cols`, `y_off`, `tx_id`, `err`), not a shared scalar — DATAFLOW processes communicate
   only through streams. A bad-size command sets the *effective* `n_rows = 0` in `load`, so
   `compute`/`store` run **zero** row-trips and the streams stay balanced (no hang), while
   the err code rides the metadata to `store`.
4. **Taps need their own channel.** A real FIR needs the `T` taps inside `compute`; routing
   them through a third `load→compute` stream (`taps_s`) keeps `compute` off `gmem` (only
   `load` reads, only `store` writes — the clean load/store split). Folding a constant tap
   array across regions would mis-lower (the original per-row sandbox hit the same rule).

---

## Finding 2 — error out via the depth-1 status stream + halt/restart

**Confirmed (csim + cosim).** The `error` scenario feeds two good 4×64 commands then a
bad-size command (`n_cols = 4096 > NCOL_MAX`):

- `load` validates the size; on failure it emits `n_rows = 0` + `err = FIR_ERR_BAD_SIZE` in
  the metadata (so **no** gmem row reads/writes happen — an absurd `n_cols` cannot overrun
  the row buffer). `store` writes that one `err` to `err_s`; `fir_dataflow` returns it.
- The top sets `error = err`, `tx_id = cmd.tx_id`, `halted = 1` and `return`s.
- **Pass 1 result:** `halted = 1`, `error = 1 (BAD_SIZE)`, `tx_id = 302`; the two good
  matrices' `Y` are bit-exact; the not-yet-run 4th command's `Y` region is **untouched**
  (still zero) — the kernel halted before it.
- **Restart:** the host clears `halted/error/tx_id` and re-issues the remaining command +
  END; the kernel runs clean (`halted = 0`), and that matrix's `Y` is now bit-exact — a
  clean restart, exactly poly's processor-restart model.

cosim error-path (RTL, two transactions — pass 1 halts early, pass 2 restarts): **PASS**. RTL
post-check: pass 1 `halted=1 error=1 tx_id=302`, both good matrices bit-exact, 4th untouched;
pass 2 restart `halted=0`, tx=303 bit-exact.

### Gotcha that cosim surfaced — `halted`/`error` are sticky `s_axilite` *outputs*

The first error cosim **failed its C-TB post-check on exactly one assertion**: `halted` read **1**
after a clean restart (everything else — the pass-1 halt, both Y's, the restart's bit-exact Y —
passed). Root cause: `halted`/`error` are `s_axilite` **output** registers the kernel only ever
*sets* (to 1, on error). poly leaves *clearing* them to the processor's pre-restart AXI write —
but in C/RTL cosim the testbench's `halted = 0` write **cannot drive a kernel-output register**,
so it read the stale `1`. **Fix (and a Phase-2 requirement this de-risk found):** the generated
top must **initialize `halted`/`error` at entry** (each fresh `ap_start` is a fresh run):

```cpp
void fir(...) {
    halted = 0; error = 0;   // <-- drive the status outputs to 0 at entry
    while (true) { ... }
}
```

poly's shell omits this and its tests never restart-and-check, so it never surfaced there. With
the entry-init the error cosim passes end-to-end and `halted` is RTL-consistent across restarts.
(The multi-transaction error tb is also the most demanding cosim case; under Vitis 2025.1's
random-stall cosim the first xsim launch is occasionally flaky, so the driver retries once — the
same retry the existing `cosim_sweep.py` uses.)

---

## Finding 3 — `m_axi` full-duplex: load-read ∥ store-write on the same `gmem` bundle

**Confirmed.** `load` reads `X` from `gmem` and `store` writes `Y` to `gmem` — the **same
single `m_axi` bundle** — and the two run as concurrent DATAFLOW processes. Evidence is
**structural + functional** (not a single-number overlap ratio — see the caveat):

- **csynth** (`waveflow_fir_dflow_proj/.../syn/report/fir_dataflow_csynth.rpt`) places `load`,
  `compute`, `store` as **concurrent sub-instances** of the `fir_dataflow` region
  (`grp_..._LOAD_ROWS_LOAD_ROW`, `grp_compute`, `grp_..._STORE_ROWS_STORE_ROW`), the per-row
  burst loops at II=1, with `load` reading and `store` writing the **same** `gmem` argument.
- **cosim** runs that region against the single shared bundle with **no contention deadlock** and
  bit-exact results — load-read and store-write coexist on one `m_axi` bundle (independent AR/R
  vs AW/W), the full-duplex property.

This reproduces the original per-row sandbox's full-duplex finding (it measured single-bundle
latency == split-bundle latency, the clean quantitative proof) now **inside the control-driven
`fir_dataflow` region** rather than a bare per-row loop.

> Caveat on absolute cycles: Vitis 2025.1 cosim ran with its **random-stall** m_axi model
> (`sim/.../fir_cosim_random_stall.json`), and each row is its own AXI burst (4 read + 4 write
> bursts/matrix), so the absolute counts below are **stall-inflated and latency-dominated** — they
> are a *throughput-period* reference, not a tight transfer-bound overlap ratio. (A clean
> overlap ratio is what the original sandbox's stall-free sweep already established.)

Concrete cycles (uniform 4×64, single shared `gmem` bundle, from `results_dflow/measurements.json`):

| scenario | matrices | cosim cycles |
|---|---|---|
| `single` | 1 | 1477 |
| `two`    | 2 | 2944 |
| `three`  | 3 | 4411 |

---

## Finding 4 — back-to-back: continuous processing with a per-command seam

**Confirmed.** N back-to-back FIR commands + END process continuously in one top
invocation (one `ap_start`), with a small per-command fill/drain bubble because the **header
read sits outside the DATAFLOW region** (the per-command barrier the plan predicted).

The per-command increment (the streaming-throughput reference, the old "Gate 3") is the
diff of the uniform-4×64 cosim points:

- `two − single` = 2944 − 1477 = **1467** cycles
- `three − two`  = 4411 − 2944 = **1467** cycles

The two diffs are **exactly equal (1467 cyc/command)** — the back-to-back **streaming period**
per 4×64 command is constant, i.e. commands process continuously and the per-command cost does
**not** grow with N (no accumulating inter-command stall). The single-command latency is
`single = 1477 = 1467 + 10`, so only **~10 cycles** of one-time pipeline fill/drain sit on top
of the steady per-command period.

The per-command barrier the plan predicted is real but **fixed**: the header read sits outside
the `#pragma HLS DATAFLOW` region, so each command pays a constant fill/drain to start/stop the
region — that cost is *inside* the 1467-cycle period (it does not compound). This is exactly the
behavior Phase 2's sim back-to-back model must reflect: a fixed per-command seam, **not**
perfectly-continuous streaming (and not a degrading one either). The constant 1467 cyc/command is
the concrete streaming reference for that model. (Absolute value is stall-inflated per the
Finding-3 caveat; the *constancy* of the increment is the robust result.)

---

## What Phase 2 is now designed against

- The poly control shell (`s_axilite halted/error/tx_id/return`, `while(1)`, END-return,
  error→halt→return) is the correct generated top for FIR, with `m_axi gmem` added. ✔
- The hook contract is `ap_uint<8> fir_dataflow(const FirCmdHdr& cmd, <gmem>, m_out)` with a
  function-scoped `err_s` (depth-1) read after a `{ #pragma HLS DATAFLOW }` block; `store`
  writes exactly one `err`. ✔
- `FIRAccel` must declare the `halted`/`error`/`tx_id` status regs so the top is fully
  port-derived. ✔ (their behavior is validated here)
- Overlap granularity is *within a matrix* (full-duplex load∥store across rows) with a
  per-command seam between commands — the sim back-to-back model keeps the seam. ✔
- Still open for Phase 2 (§2): swap the 7-word hand header for the built-in
  `FIRCmd.read_axi4_stream<32>`, and move gmem to `ap_uint<mem_dwidth>*` + the
  `read_array_slice` / `write_array_slice` element-coordinate family (this sandbox uses a
  plain `float*` and direct indexing, sufficient for the structural de-risk).

## Reproduce

```bash
cd examples/rowwise_fir/sandbox
PYTHONPATH=../../.. ../../../pysilicon-venv/Scripts/python.exe run_dflow.py --smoke   # csim + csynth, all scenarios
PYTHONPATH=../../.. ../../../pysilicon-venv/Scripts/python.exe run_dflow.py --cosim   # + RTL cosim (bit-exact, halt/restart, cycle points)
# -> results_dflow/measurements.json
```
