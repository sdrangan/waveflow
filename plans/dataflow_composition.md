# Plan — multi-module DATAFLOW composition + LT/AT fidelity for internal kernel structure

**Status:** design captured 2026-06-21 (from the docs-vmac-mmqueue session). Not started.
**Branch context:** the `docs-vmac-mmqueue` docs branch is being closed as-is — VMAC's kernel is
memory-bandwidth-bound (**II≈19** at PF=1; the fitted cycle model is `depth≈43 + II·trips`,
`II≈19`), documented honestly. The kernel rework routes through *this* plan, not a patch to VMAC.

## Why this exists — the finding that motivates it

VMAC's inner loop reads A and B **interleaved at the word level** on one `m_axi` port:
`A[0], B[0], A[1], B[1], …` — two address streams. An AXI burst needs monotonic sequential
addresses, so this **never coalesces into a burst**; every element pays address-phase latency
→ the csynth report shows the inner loop `VITIS_LOOP_147_3` at **II=15** (iteration latency 16),
amortizing to ~19/trip end-to-end. The burst-fail reasons in the report are literally the runtime
conditionals: *"Access load is in the conditional branch"* (the predicated B read),
*"Access store is in the conditional branch"* (the predicated Y write), *"Stride is incompatible"*.

**Key correction (do not re-attempt):** merely making the inline accesses *unconditional*
(remove `ab_eq`/`need_b`/`reduce` predicates, dummy addresses, predicate only the store) is
**necessary but NOT sufficient** — it unblocks the burst *analyzer* but the **access order** is
still burst-hostile (still two interleaved streams). So a uniformized inline loop would still sit
well above the floor. The principled fix is **load-compute-store with resident row buffers**.

## The principled structure: row-buffered load-compute-store dataflow

Split the accelerator into three concurrent modules over a shared, double-buffered, partitioned
BRAM row buffer:
- **Load** reads a whole row of each operand as a **contiguous burst** (sequential addresses →
  full AXI efficiency) into a ping-pong BRAM buffer; signals "row ready" over a stream handshake.
- **Compute** reads the row buffer from BRAM (partitioned → parallel lanes), runs the datapath at
  **II=1**, writes the output row buffer.
- **Store** writes the output row as a contiguous burst.
- Double-buffering (ping-pong, ~2·ncol_max per operand) overlaps the three stages across rows.

**The II model (single shared `m_axi` port) — CORRECTED by Phase 1 cosim (2026-06-22):** the floor
is **per-direction**, not per-total-stream. An AXI `m_axi` bundle is **full-duplex** — independent
AR/R (read) and AW/W (write) channels — so a read stream and a write stream on the *same* bundle
**never contend**. The floor is:

> **II floor (single bundle) = max(#read-streams, #write-streams).** Separate bundles only
> parallelize **same-direction** streams.

| streams / element | reads | writes | II (single bundle) |
|---|---|---|---|
| A only (read A, accumulate, no Y write) | 1 | 0 | **1** |
| A+B (read A,B + reduce) — *VMAC* | 2 | 0 | **2** (two reads share the read channel) |
| A+Y (read A + write Y) — *FIR* | 1 | 1 | **1** (full-duplex; **NOT** 2) |
| A+B+Y (read A,B + write Y) | 2 | 1 | **2** (set by the two reads) |

Compute is *free* (II=1, BRAM). **Empirical correction (Phase 1):** FIR (1 read + 1 write) cosims
**byte-for-byte identical** on single-bundle vs split X/Y bundles — there is no II=2 and the
multi-bundle knob buys nothing for it. The `#streams→II` floor (VMAC's II≈19) is a **same-direction**
phenomenon (two interleaved *reads* sharing the one read channel); splitting same-direction streams
onto separate bundles is the only case the multi-bundle knob helps. **Decision: the multi-bundle
lesson is dropped from this arc** — it is not important, and FIR cannot demonstrate it (its two
streams are opposite-direction). If ever wanted, it lives with VMAC (same-direction reads), not here.
**Caveat (paper-relevant):** single==split is a truth *at the AXI interface as cosim models it*;
real DDR shares read/write bandwidth + pays turnaround, so hardware would show some R/W contention
cosim does not — a genuine fidelity boundary of cosim-calibration ([[reference-paper-positioning]]).

This is the hardware realization of Waveflow's SimPy **parallel-process** model: three SimObjs over
channels ⇒ three functions in a `#pragma HLS DATAFLOW` region. Sim concurrency = kernel concurrency
(a sim/synth correspondence worth teaching, and consistent with [[feedback-vitis-sequential]]).

## The vehicle: a per-row FIR example (`FIRAccel`)

Real-valued **floating-point FIR per matrix row**: `Y[i,j] = Σ_{t} h[t]·X[i, j−t]`, taps `T`.
Why FIR (better than the inner-product) — **the sliding window forces the resident row buffer**:
you cannot FIR a pure stream (you need windowed access `X[i, j−t]`), so the row *must* be resident
and randomly addressable. Load-compute-store becomes a *correctness necessity*, not a nicety —
the cleanest possible motivation. It also teaches the **BRAM-channel vs FIFO-channel** distinction:
the load→compute channel must be a partitioned-BRAM ping-pong (windowed random access); the
compute→store channel can be a FIFO (sequential).

Modeling: **three HwComponents** `FIRLoad` / `FIRCompute` / `FIRStore` with a shared partitioned
BRAM, composed in a top `FIRAccel`. Command (streaming): filter coefficients `h[T]`, address of
`X`, `n_rows`, `n_cols`. Output `Y`. Compute II=1; one read (X) + one write (Y) → opposite
directions → full-duplex → **II=1** on a single bundle (Phase 1 cosim: single==split byte-for-byte).

**FIR's real job (the reframe):** be the *canonical, generalizable load-compute-store template* — for
students and, crucially, as the worked example **AI codegen learns from to build other
load-compute-store accelerators and simulate them at both fidelities** ([[project-hook-authoring-docs]]).
What it teaches: the 3-stage structure, BRAM-ping-pong vs FIFO channels, the per-row-overlap latency
model, and the block↔structural fidelity demo. It does **not** teach the multi-bundle/#streams knob
(dropped — see above).

**Pinned-but-unanswered params (decide at build time):**
1. **Edge handling** — `valid` (output `ncol−T+1`, no edge cases — leaning this) vs `same` (zero-pad, output `ncol`).
2. **Port topology** — single shared `m_axi` bundle. (Phase 1 settled this: split X/Y bundles are
   byte-for-byte identical because the bundle is full-duplex, so single-bundle is the canonical form;
   no multi-bundle variant needed.)
3. **Sizes** — taps `T`, `ncol_max` (BRAM depth), e.g. `T=8`, `ncol_max=1024`. Float confirmed.

## The framework design: two orthogonal axes (the deep contribution)

This is **LT↔AT escalation applied to a kernel's *internal* structure**. Separate *how it's
realized in hardware* from *how much of it the sim models*:

**`exec_model`** (internal realization, drives codegen):
- `extract` — `run_proc` → one function (today's default)
- `hook(impl_file)` — hand-written `.tpp` (today)
- **`dataflow(subcomponents, channels)`** — NEW: a sub-graph lowered to a `#pragma HLS DATAFLOW` top
- **`vendor(kernel_ref, timing)`** — NEW: a black box; codegen emits a *call* into vendor IP

**`sim_fidelity`** (orthogonal, drives the SimPy event structure):
- `block` — one event per op; time advanced by the timing model (per-matrix, the load-compute-store
  interleaving abstracted out — lives only in the latency model)
- `structural` — sub-component events, emergent interleaving + timing (per-row; needs a sub-graph)

**The timing model is the shared currency between fidelities:** the `structural` run is what you
*calibrate* (cosim it, fit `cycles(nrow, ncol, T)`); the `block` run *consumes* that fit. The fine
model produces the abstraction the coarse model uses — the VMAC cosim-calibration loop
([[project-cycle-model-training]], [[project-poll-until-lt-model]]), now closing *across fidelities
of the same component*. The refined latency form — **CORRECTED by Phase 1 cosim to bilinear** (per-row
`#pragma HLS DATAFLOW` overlaps rows, so the per-row interval scales with `n_col`):
`L0 + L_row·n_row + L_col·n_col + II·trips`. The per-output rate is **n_row-dependent** (Phase 1: 2.44
cyc/output at n_row=1 vs 1.25 at n_row=4 — that gap *is* the row-overlap amortization), approaching an
asymptotic floor of ≈1 cyc/output (compute II=1 + full-duplex load/store) as n_row grows. **Calibration
lesson:** the model is genuinely bilinear, so the structural-fidelity fit needs **≥3 n_row values** and
an **interior** held-out point — Phase 1's `{1,4}`-only grid with a 2× `n_col` extrapolation gave a
misleading 19% holdout (under-sampling + extrapolation, not just model-form error).

### Answering "how to represent a Vitis internal-DATAFLOW kernel in Waveflow Python"
The internal DATAFLOW is an **implementation detail behind the component's interface** — the
component/interface boundary is the seam that hides it. Choose how much of the inside to model:
- **You build it (FIR):** declare it **structurally** (`dataflow` exec_model: sub-components +
  internal channels). Codegen lowers the sub-graph to a DATAFLOW region. Sim runs `structural`
  *or* collapses to `block`.
- **Vendor block (GEMM, Vitis L1 libs):** declare a **`vendor` black box** — external ports +
  golden + a timing model (datasheet or one-time cosim). Codegen emits a call to the vendor kernel;
  sim is `block`-only. **The internal DATAFLOW is never represented — that is the value.** This is
  the "pre-built wrapper = a hook calling vendor IP" path ([[project-vitis-dsp-lib-wrap]]) and the
  "block-LT HwComponent" framing ([[project-rfsoc-sdr-direction]]). GEMM is the canonical case:
  matrix-wise sim + abstracted latency, without rebuilding its load-compute-store.

**Killer teaching result for the FIR example:** run the *same* `FIRAccel` at both fidelities, prove
identical `Y`, and prove the `block` latency — calibrated from the `structural` cosim — tracks the
`structural` timeline. "Pick your fidelity; the coarse one is the calibrated shadow of the fine one."

## The hard open problems (name them before coding)

1. **Internal channel types.** Load→compute = partitioned-BRAM ping-pong (windowed); compute→store
   = FIFO. Waveflow needs an internal-channel interface lowering to dataflow **array/stream
   channels** (not AXI). Squarely the [[project-memory-modeling-unification]] seam (BRAM-backed
   Region / element-coordinate access) — FIR is its first real consumer.
2. **DATAFLOW codegen** from a sub-graph (emit channel decls + the N calls + `#pragma HLS DATAFLOW`)
   — net-new; the multi-module-on-a-dataflow-region milestone.
3. **Sim-faithfulness** — does the SimPy structural interleaving match Vitis's dataflow schedule?
   That is the cosim check the example performs (and the reason to build the hand-sandbox first).

## Sequencing & where it lives in the curriculum

This is a new teaching **axis — kernel composition / dataflow**, NOT the next host-interface
pattern. It belongs in the hardware-generation arc (near `comp_codegen` / `custom_hooks`), not as
slot 6 of the interface progression. VMAC later *applies* the pattern as an optional "putting it
together" follow-up (and gets its fast kernel) — only after the dedicated example establishes it.

**Decision (from the session): jump straight to the 3-module FIR example** rather than a contrived
2-module warmup — load-compute-store is the self-motivating minimal case; introduce the
dataflow/stream/double-buffer machinery in that context. Fall back to a 2-module warmup only if the
FIR example feels like it teaches two things at once.

## The four-step program (do not conflate)

1. **Hand-written HLS sandbox** — the float FIR load/compute/store `.cpp` (3 functions + DATAFLOW +
   ping-pong partitioned BRAM + row-ready stream handshake). csynth + a tiny cosim to **confirm
   II=1 compute** (✅ done; the II=2-single-port target was *refuted* — full-duplex, see § OUTCOME)
   and to **produce the cosim numbers that calibrate the
   `block` timing model**. Lowest-risk; grounds everything downstream; commit as a teaching
   artifact ("how to build load/compute/store dataflow + validate II in a sandbox"). **Do this
   first — nothing else is coded until the `.cpp` proves the II.** **Full execution spec:
   [§ Phase 1 — execution spec](#phase-1--execution-spec-hand-written-hls-sandbox) below.**
2. **Framework codegen** — `dataflow` exec_model: multi-HwComponent composition with shared
   BRAM/stream channels → `#pragma HLS DATAFLOW` top.
3. **The Waveflow example** — `FIRLoad`/`FIRCompute`/`FIRStore`/`FIRAccel` components generating (1);
   the `block`↔`structural` fidelity demo + the calibration. **Calibration protocol** (see
   `plans/load_compute_store.md` § Calibration protocol): single-task isolation, ≥3 `n_row` values,
   in-range `n_col`, an **interior** holdout, and a `sklearn.linear_model.LinearRegression` fit of the
   bilinear `L0 + L_row·n_row + L_col·n_col + II·trips` (scikit-learn is now a core dependency).
4. **Docs** — the kernel-composition arc page(s); and the deferred forward-note below.

## Phase 1 — execution spec (hand-written HLS sandbox)

> **⚠️ SUPERSEDED IN PART — see the ✅ OUTCOME block at the end of this section.** This spec was
> authored *before* Phase 1 ran. Its II=2 single-port / II=1 split-bundle targets were **refuted** by
> cosim (single bundle is full-duplex → single==split → II=1); the latency model is **bilinear**, not
> single-II. The text below is kept as the as-authored spec; trust the OUTCOME block for what is true.

**Goal.** A self-contained, hand-written HLS load-compute-store FIR kernel that (a) **proves** the II
claims of this plan empirically (II=1 compute, II=2 single-port memory, II=1 split-bundle), and
(b) **produces the cosim-calibrated `cycles(n_row, n_col, T)` numbers** the later `block` fidelity
will consume. Nothing in Phases 2–4 is coded until this `.cpp` proves the II. This phase touches
**no Waveflow Python framework code** — it is hand-authored HLS + a numpy data/golden generator + a
tiny results extractor. Commit it as a teaching artifact.

### Locked decisions for the sandbox (resolve the plan's "pinned-but-unanswered" params for v1)
- **Edge handling:** `valid` only. Output length per row = `n_col − T + 1`. No zero-pad, no edge
  cases. (`same` is deferred to the Waveflow example if ever wanted.)
- **Datatype:** `float` (IEEE-754 single). Golden computed in C++ accumulation order so csim is
  **bit-exact**, not tolerance-based (see Acceptance).
- **Sizes:** `T = 8` taps; `NCOL_MAX = 1024` (BRAM row-buffer depth bound); coefficients `h[T]`
  passed as a kernel arg (not compile-time baked), matrix base addr + `n_rows` + `n_cols` as args.
- **Port topology:** build **two** csynth solutions from the *same* sources via a compile flag —
  `solution_singleport` (one shared `m_axi` bundle for X and Y → II=2) and `solution_split`
  (`#define WF_FIR_SPLIT_BUNDLE`, separate `m_axi` bundles for X and Y → II=1). The pair is the
  whole point: it teaches single-bundle II = #streams vs multi-bundle II = 1.
- **Part / clock:** `xc7z020clg484-1`, `create_clock -period 10` (match `block_scale`/`vmac`).

### Folder layout — `examples/rowwise_fir/sandbox/`
(parent `examples/rowwise_fir/` stays empty until Phase 3 fills it with generated Waveflow files;
commit the sandbox at its final home, do not relocate later)

```
examples/rowwise_fir/sandbox/
  README.md                 # what this is, how to run, what it proves; links back to this plan
  fir_sandbox.hpp           # params (T, NCOL_MAX, N_PINGPONG=2), typedefs (real_t=float), fn prototypes
  fir_sandbox.cpp           # the 3 functions + DATAFLOW top `fir_accel`
  fir_tb.cpp                # testbench: load data/ , run fir_accel, compare vs Y_golden.bin (bit-exact)
  run.tcl                   # drives BOTH solutions: csim + csynth(x2) + optional cosim
  gen_data.py              # numpy: writes data/{X.bin,h.bin,Y_golden.bin,meta.json}
  cosim_sweep.py           # drives run.tcl over a (n_row,n_col) grid, fits the cycle model
  extract_results.py       # parse csynth rpt -> results/csynth_*.json via waveflow.utils.csynthparse
  data/                     # committed small fixtures (one representative case for csim)
  results/                  # committed artifacts (the deliverables — see below)
  .gitignore                # ignore waveflow_rowwise_fir_proj/ build dir, *.log
```

### Kernel structure (`fir_sandbox.cpp`)
Top `fir_accel` is a `#pragma HLS DATAFLOW` region streaming one row at a time through three
concurrent functions over a **double-buffered, partitioned BRAM** row buffer:

```
// --- channels ---
//  load -> compute : ping-pong partitioned BRAM   real_t xbuf[N_PINGPONG][NCOL_MAX]   (windowed random access)
//                    + hls::stream<row_meta> for the "row ready" handshake (which buffer, n_col)
//  compute -> store: hls::stream<real_t> yfifo     (sequential -> a FIFO, NOT BRAM)
//                    + hls::stream<row_meta> for "row done"

void fir_load   (const real_t* X, int n_rows, int n_cols, real_t xbuf[N_PINGPONG][NCOL_MAX],
                 hls::stream<row_meta>& ready);          // contiguous burst read of n_cols words/row
void fir_compute(const real_t h[T], real_t xbuf[N_PINGPONG][NCOL_MAX],
                 hls::stream<row_meta>& ready, hls::stream<real_t>& yfifo,
                 hls::stream<row_meta>& done);            // II=1 inner loop, n_cols-T+1 outputs/row
void fir_store  (real_t* Y, hls::stream<real_t>& yfifo, hls::stream<row_meta>& done); // burst write

void fir_accel(const real_t* X, real_t* Y, const real_t h[T], int n_rows, int n_cols) {
#pragma HLS DATAFLOW
    // xbuf: #pragma HLS ARRAY_PARTITION (cyclic, factor>=T) on dim=2 for windowed parallel reads
    // single-port v1: X and Y share one bundle:  #pragma HLS INTERFACE m_axi port=X bundle=gmem ...
    //                                            #pragma HLS INTERFACE m_axi port=Y bundle=gmem ...
    // split  variant: bundle=gmemX / bundle=gmemY under #ifdef WF_FIR_SPLIT_BUNDLE
    ...
}
```
Required pragmas to hit the targets: `PIPELINE II=1` on the compute inner loop; `ARRAY_PARTITION`
on `xbuf` dim=2 (cyclic, factor ≥ T) so the T-tap window reads in parallel; `m_axi`
`max_read_burst_length`/`max_write_burst_length` ≥ a row so the load/store coalesce; double-buffer
via `xbuf[N_PINGPONG][...]` (`N_PINGPONG=2`) indexed by `row % N_PINGPONG`.

### Data + golden (`gen_data.py`, numpy)
Writes little-endian `float32` raw `.bin` (the convention `csim_design -argv "$data_dir"` already
uses): `X.bin` (`n_rows × n_cols` row-major), `h.bin` (`T`), `Y_golden.bin`
(`n_rows × (n_cols − T+1)`), and `meta.json` (`n_rows, n_cols, T, dtype, layout`). Golden:
`Y[i,j] = Σ_{t=0..T-1} h[t]·X[i, j+T-1-t]` accumulated **left-to-right in the same order the C++
compute loop accumulates** so the float result is bit-identical. Commit one representative fixture
(e.g. `n_rows=4, n_cols=64`) under `data/` for csim; the sweep generates its own temp data.

### Driver (`run.tcl`) — mirror `examples/block_scale/run.tcl` conventions
- `WAVEFLOW_SUCCESS:` / `WAVEFLOW_ERROR:` sentinel lines; `exit 0/1`.
- `csim_design -argv "$data_dir"`; then **two** `csynth_design` runs (single-port solution, then a
  reset solution with `-D WF_FIR_SPLIT_BUNDLE` in cflags).
- Cosim gated behind `WAVEFLOW_ROWWISE_FIR_COSIM` env (off by default, like block_scale), trace
  level behind `WAVEFLOW_ROWWISE_FIR_TRACE_LEVEL`. Cosim runs against the single-port solution.

### Calibration sweep (`cosim_sweep.py`) — the numbers Phase 3's `block` model consumes
Sweep the **single-port** solution over a `(n_row, n_col)` grid, run cosim, record measured latency
cycles per point, and fit the refined model from this plan:

> `latency_cycles ≈ L0 + n_row·L_row + II·trips`, with `trips = n_row·(n_col − T + 1)` (output
> elements) and the expectation **II ≈ #external-word-streams = 2** for single-port once `n_col` is
> large enough to amortize `L_row` (per-row burst setup + dataflow handshake).

- **Grid:** `n_col ∈ {16, 32, 64, 128, 256, 512}` × `n_row ∈ {1, 4}` for the fit; hold out one
  point (e.g. `n_row=4, n_col=1024`) and report its rel-err — **mirror vmac's held-out discipline**
  (target ≲ 2%, the `sim_vs_cosim` bar). The small-`n_col` points are what expose `L_row` (and are
  exactly why small matrices *look* like high II).
- Emit `results/cosim_sweep.json` in the **same shape as `examples/vmac/timeline/sim_sweep.json`**
  (`calibration: {L0, L_row, ii, r2, holdout_*, model, trips_formula}`, `points: [...]`,
  `clk_period_ns`) so downstream tooling and docs are consistent.

### Deliverables (committed under `results/`) — "all the results we need"
1. `results/csynth_singleport.json` and `results/csynth_split.json` — parsed via
   `waveflow.utils.csynthparse.CsynthParser`: top II/latency, **compute-loop II (=1)**, the m_axi
   burst-inference lines (proving the load/store coalesce), and the top II (=2 single / =1 split).
2. `results/cosim_sweep.json` — the grid points + fitted `{L0, L_row, II, r2}` + held-out rel-err.
3. `results/fir_sandbox_results.md` — human-readable summary: a table of the three II claims with
   measured vs expected, the fitted cycle model with R² and held-out error, and the burst-coalesce
   evidence. This is the artifact a reader/paper cites.
4. `README.md` — how to regenerate everything (`gen_data.py` → `run.tcl` → `cosim_sweep.py` →
   `extract_results.py`), and a one-paragraph statement of what the sandbox proves.

### ✅ Phase 1 — OUTCOME (completed 2026-06-22, branch `rowwise-fir-sandbox`, commit `cb2a654`)
Built at `examples/rowwise_fir/sandbox/` (per-row DATAFLOW: `fir_load_row` → BRAM ping-pong + tap
channel → `fir_compute_row` → FIFO → `fir_store_row`). Results committed under `results/`. Status of
the originally-authored acceptance items (two were **refuted with evidence** — see the corrected II
model above):
- [x] **csim + cosim bit-exact** vs numpy golden — met.
- [x] **Compute II=1** (and load/store/tap loops II=1); **AXI bursts inferred** on X read and Y write — met.
- [~] ~~Single-port floor II=2~~ — **REFUTED.** Single-bundle is full-duplex; single==split
      byte-for-byte (272@1×64, 2383@4×256). No II=2.
- [~] ~~Split-bundle II=1 distinct from single~~ — **REFUTED.** Identical to single (no contention to
      relieve).
- [~] ~~Model `L0 + n_row·L_row + II·trips`, R²≳0.999, holdout ≲2%, II≈2~~ — **mis-specified.** Latency
      is **bilinear**: `≈ 68.6 + 60.2·n_row + 1.03·n_col + 1.49·trips` (R²=0.987). Steady-state
      ≈1.25 cyc/output at n_row=4 (n_row-dependent; asymptotic floor ≈1). The 19% holdout = thin
      (`n_row∈{1,4}`) grid + 2× `n_col` extrapolation; refit with ≥3 n_row + interior holdout.
- [x] Artifacts committed; build dir gitignored; README regenerable — met.

**Carry-forward truth for Phases 2–4:** compute II=1, full-duplex single bundle (read+write never
contend at the AXI level), bilinear per-row-overlap latency, ≈1.25 cyc/output finite-size throughput.

### Explicitly OUT of scope for Phase 1 (do not start these here)
No Waveflow `HwComponent`/codegen (`FIRLoad`/`FIRCompute`/`FIRStore`/`FIRAccel`) — that is Phase 3.
No `exec_model=dataflow` / `sim_fidelity` framework work — Phases 2–3. No `same`-edge handling, no
`int`/`fixed`/`complex` datatype, no >1 `m_axi` read operand. The sandbox is a *ground-truth kernel*
the framework will later be shown to reproduce, nothing more.

## Loose ends from the docs-vmac-mmqueue branch

- **Deferred mmqueue forward-note.** A short honest note in the VMAC docs (`codegen.md`/`timing.md`)
  — "this kernel is memory-bandwidth-bound (II≈19, interleaved A/B access can't burst); the
  principled high-throughput structure is the [FIR load-compute-store example]" — should be added
  **once the FIR example exists** (a forward link now would dangle). It turns VMAC's limitation into
  the narrative bridge to this arc.
- VMAC is NOT to be apologized for: the mm-queue example's job is the LT model + cosim calibration,
  which faithfully models *whatever* the hardware does. A memory-bound SUT is valid.

## Connections (existing project context)

[[project-cycle-model-training]] (calibrate timing from cosim) · [[project-vitis-dsp-lib-wrap]]
(vendor black-box hook) · [[project-memory-modeling-unification]] (BRAM channel interface) ·
[[project-rfsoc-sdr-direction]] (block-LT HwComponents) · [[reference-paper-positioning]]
(LT + cosim-calibration is the niche — `block`/`structural` fidelity is a concrete instance) ·
[[project-vmac-mm-queue-timing]]-style work / `plans/vmac_mm_queue_timing.md` (the LT→AT escalation
this generalizes).
