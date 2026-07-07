# Implementation Roadmap — Hierarchical Dataflow + the Interleaver Examples

This is an **executable plan** for Claude CLI. Work the phases **in order**. Each phase has a
**Gate** (an objective check). **Do not proceed past a failed gate** — stop and report. The design
rationale lives in the companion docs; this file is the sequencing, the acceptance gates, and the
hard-won constraints so you don't re-derive them.

Companion design docs (read these for depth, don't duplicate them):
- `plans/dataflow_mod.md` — DATAFLOW codegen design + the C1–C7 codegen-shape de-risk (with cosim
  numbers) + the capability model + the host-contract menu.
- `plans/component.md` — hierarchical `HwComponent`, the capability/target/role framework, the
  verification ladder, active/passive taxonomy, interface lowering, SystemC/IPI codegen.

Golden reference for the interleaver: `examples/interleaver/sandbox/il_1d/` (**gitignored** — present
in this working tree, not in a fresh clone). Hand-written, cosim-validated variants `interleaver_c{1..7}.cpp`
+ `run_interleaver.py`. The **validated synthesizable form is C1/C3/C6**: a counted `for (j<nj)` loop
with `#pragma HLS DATAFLOW` in the body, per-job command read fused in, **pure-write store**, single
`mem` `m_axi` port, `ap_ctrl_hs` — cosims clean at **period 286 ≈ n @ MEM_DW=64**, bit-exact.

## Environment / prerequisites

- **venv**: `../pysilicon-venv` (sibling; Python 3.14). Run tests as
  `../pysilicon-venv/Scripts/python.exe -m pytest ...`. A fresh Bash shell defaults to system Python
  without deps — using it makes "0 failed" meaningless.
- **Vitis HLS 2025.1 is installed** — `-m vitis` tests genuinely run. Watch for soft-skips that mask
  failures (a real skip prints `s`, a real pass `.`).
- **Baseline**: `main` has ~15 known-failing non-vitis tests (test_build×9, dataschema_poly×1, poly
  timing×5). A branch is clean iff its failures are a **subset** of main's — verify by diffing a
  fresh `main` run, don't trust "pre-existing" labels.
- **Working-tree state**: this session left uncommitted de-risked work — the array-utils pure-write
  codegen (`waveflow/hw/arrayutils.py` + `tests/hw/resources/arrayutils_slice_test.cpp`) and the plan
  docs. Phase 0 commits it. The interleaver sandbox is gitignored on purpose.
- **Solo-dev git flow**: one branch, multiple commits; do NOT open multiple PRs. Commit messages end
  with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

## Hard constraints (session findings — treat as given, do NOT re-explore)

These were each proven by cosim this session. Re-deriving them wastes ~5-min cosim cycles.

1. **DATAFLOW overlap ⟺ a counted loop.** Proven by variants (interleaver, MEM_DW=64, NJOBS=4):
   - C1 (`#pragma HLS DATAFLOW` in a counted `for`) → overlaps, **286**.
   - C2 (region as a called `il_job()` function) → **serializes, 813**. A DATAFLOW region cannot be a
     callable function; the pragma + stages must be inline in the loop body.
   - C3/C4 (per-job command read fused inside the loop, before or after the pragma) → overlaps, 287.
   - C5 (`while(1)` + inline sentinel `break`) → **serializes, 814**. A data-dependent early exit in
     the dataflow loop kills the overlap.
   - C6 (sentinel `break` in a *parse* loop, then a counted `for`) → overlaps, **286**. This is how a
     sentinel protocol keeps overlap.
   - Host contract: **count-header** (`nj` as a stream/`s_axilite` word, then `for(j<nj)`) is the
     default — streams job-by-job AND overlaps. Sentinel = parse-then-count (buffer-all barrier),
     only when `nj` is unknown. Never expose a bare unbounded loop.
2. **Pure-write store keeps overlap on a shared `m_axi` port; an RMW store serializes it.** The store
   must not *read* memory (even dead boundary-RMW). `write_array_slice` is now pure-write by default;
   `write_array_slice_rmw` is the rare read-modify-write variant. This is already implemented in
   `waveflow/hw/arrayutils.py` (Phase 0 commits it).
3. **`hls::task` + `m_axi` csynths fine but cannot be Vitis C/RTL cosim'd** (`COSIM 212-345`:
   `ap_ctrl_none` cosim only supports combinational / II=1 / pure-stream). It IS synthesizable
   (correcting the old "impossible" claim); it must be verified in **SystemC/xsim or Vivado**, not
   Vitis cosim. Harness gotchas: `hls::task` bodies are single-firing (runtime re-fires; no internal
   `while(1)`), and the TB must **blocking-read** an output/`done` stream to sync (an `empty()` check
   races the detached task).
4. **cocotb cannot drive Xilinx xsim** (no VPI; xsim absent from cocotb's supported-sim list). So RTL
   verification here is **SystemC-in-xsim** (Vivado's `xsc`/`xelab`/`xsim`), not cocotb. cocotb would
   need Verilator (can't compile encrypted Xilinx IP) or a Questa/Riviera license.
5. **Two interleavers, two lessons** (see the rename in Phase 4 / Phase 5):
   - `interleaver_df` — bounded, `#pragma HLS DATAFLOW`, `ap_ctrl_hs`, **fixed `njobs` per
     `ap_start`**, Vitis-cosim-able. The basic dataflow example.
   - `interleaver` — free-running, `hls::task`, data-driven sentinel termination, SystemC-verified.
     The full-hierarchy driver.

## Rules of engagement

- **Measure, don't trust.** Every codegen milestone is validated by cosim/test, not by inspection.
  This whole plan exists because assumptions (C2, C5, hls::task+m_axi, cocotb+xsim) were overturned
  by measurement.
- **Stop at a failed gate.** Report the failure; do not build on top of an unverified rung.
- **Keep poly + histogram green.** Any change to the base `HwComponent`/endpoint/codegen must not
  regress `examples/stream_inband` (poly) or `examples/shared_mem` (histogram). They are the
  regression anchors (Option 2's real content, folded in — do NOT rewrite them into the new framework
  now; just keep them passing).

---

## Phase 0 — Commit the de-risked base

**Goal:** bank this session's validated work so later phases build on a stable, committed base.

**Steps:**
1. On a branch (not `main`), commit in two logical commits:
   - **array-utils codegen**: `waveflow/hw/arrayutils.py` (pure-write `write_array_slice` default +
     `write_array_slice_rmw` + read peel/pipeline + zero-pad tail) and the test update
     `tests/hw/resources/arrayutils_slice_test.cpp` (case C now calls `write_array_slice_rmw`).
   - **plans**: `plans/dataflow_mod.md`, `plans/component.md`, `plans/interleaver_roadmap.md`, and the
     `.gitignore` entry for `examples/interleaver/sandbox/`.

**Gate:**
- `../pysilicon-venv/Scripts/python.exe -m pytest tests/hw/test_arrayutils_slice_vitis.py tests/hw/test_arrayutils_slice_ii_vitis.py -m vitis` → **all pass** (validated 12/12 this session).
- poly (`examples/stream_inband`) and histogram (`examples/shared_mem`) still build + cosim.

---

## Phase 1 — Endpoint direction-as-capability (shared base)

**Goal:** implement `dataflow_mod.md` Phase 1 — the capability layer both plans share.
- `@port_read` / `@port_write` method tags on the `InterfaceEndpoint` base; tag the concrete
  endpoints (FIFO/AXIS master+slave; memory master `read_slice`/`read_lane`/`write_slice`).
- `endpoint.as_dir('R'|'W'|'RW')` → a capability **proxy** exposing only the matching method subset
  (or full for `'RW'`).
- Codegen: a read-only bound endpoint emits a `const` pointer in the kernel signature.

**Gate (no Vitis needed — fast):**
- Unit tests: a read proxy raises on any write call; a write proxy raises on any read call; the
  generated signature carries `const` for an `'R'` binding.
- **poly + histogram stay green** (this touches the base — the regression anchor check).

---

## Phase 2 — `VitisDataflow` pysim model

**Goal:** `dataflow_mod.md` Phase 2. `DataflowStep(proc, inputs, outputs, if_dir)` +
`VitisDataflow(steps)` `SimObj` with a `simpy.Store` per internal edge (`capacity = pipo_depth`,
default 2), `put()`, backpressure. Per-edge channel type (PIPO vs stream). Per-step timing model
(reuse `waveflow/calib/`).

**Gate:** pysim of the interleaver region reproduces the cosim throughput (period ≈ 540 @dw32, ≈ 286
@dw64) within tolerance, with the overlap **emerging from queue depth** (no end-to-end fit). Compare
against the sandbox cosim numbers.

---

## Phase 3 — DATAFLOW codegen (`interleaver_df`)

**Goal:** `dataflow_mod.md` Phase 3. Generate the **C1/C3/C6 shape**: `for (j<nj) { #pragma HLS
DATAFLOW; <read cmd>; load; gather; store; }`, count-header (`nj` upfront), internal edges → loop-local
PIPO arrays / `hls::stream`, `if_dir` endpoints → `m_axi` (`const` for read), stores via pure-write
`write_array_slice`. Control plumbing via the existing poly/regmap `on_start` extraction.

**Gate:** the **generated** interleaver kernel cosims **bit-exact** and matches the hand-written
sandbox throughput (286 @dw64, ping-pong overlap in the VCD). Structurally diff generated vs
`examples/interleaver/sandbox/il_1d/interleaver.cpp`.

---

## Phase 4 — Ship `interleaver_df` as a Waveflow example

**Goal:** `dataflow_mod.md` Phase 4, named **`interleaver_df`**. Full example, `shared_mem`/`hist`
anatomy: `IlAccel(HwComponent)` + `IlCmd` schema (`MemAddr` byte addresses) + the `VitisDataflow`
region + generated hook; `BuildDag` + `run_dag` CLI; LT sim consumer; MEM_DW sweep. **Document the
limitation** prominently: fixed `njobs` per `ap_start`, no data-driven early termination (that's the
free-running `interleaver`).

**Gate:** example builds + cosims; LT sim matches cosim (Gate); MEM_DW sweep reproduces the 2n→n
story (540→286); committed figure regenerable without Vitis. Retrofit poly/hist to the shared
primitives only if cheap.

---

## Gate G1 — SystemC-in-xsim smoke test (BEFORE the free-running interleaver)

**This is the critical gating de-risk.** The free-running `interleaver` can be verified *only* via
SystemC/xsim (constraint 3). We have NOT proven that flow works on Windows. Prove it before investing.

**Steps:**
- Write a ~10-line SystemC TB (`sc_module` with an `SC_THREAD`) driving a trivial Verilog DUT (e.g., a
  registered adder). Compile with Vivado's `xsc` (`C:/Xilinx/2025.1/Vivado/bin/xsc`), elaborate with
  `xelab`, run in `xsim`. Confirm it runs and checks output.
- Separately confirm `#include "ap_int.h"` (from `C:/Xilinx/2025.1/Vitis/include`) compiles under
  `xsc`, and `ap_int ↔ sc_bv` conversion works.

**Gate:** the `xsc → xelab → xsim` SystemC flow runs a passing check on Windows, and `ap_int.h`
compiles under `xsc`.
- **If PASS** → proceed to Phase 5.
- **If FAIL** → STOP. Do not build the free-running path. Report the specific blocker; the decision
  becomes "Linux box / Questa license / defer" — not "build codegen we can't verify."

---

## Phase 5 — Free-running `interleaver` (the `component.md` hierarchy driver)

**Only if Gate G1 passed.** This is where the full `component.md` framework gets built, driven by a
real example: free-running load-compute-store with data-driven early termination.

**Scope (see `component.md` for the design):**
- Hierarchical `HwComponent`: `add_comp` / `add_if` / `bind`; load/compute/store as **active**
  sub-components; a **stream-wrapped memory** (passive) with arbitration for the shared `m_axi`.
- Capability/target/role: `vitis_synthesizable()` recursion (bottom-up) + role assignment (top-down);
  the composite-kernel role via `hls::task` (single-firing task functions + streams + the read/write
  DMA-task pattern for `m_axi`).
- SystemC codegen (`SCCodegenStep` → `sc_module` / `SC_THREAD`), the AXI-MM slave memory model, the
  single shared golden header.

**Gate:** the generated free-running `interleaver` (a) csim + csynth clean, and (b) **RTL-verified in
SystemC/xsim** (bit-exact against the pysim golden) — since Vitis cosim will refuse it (212-345).

---

## Later (not in this roadmap's critical path)

- Vivado IPI codegen (`component.md`) — the build path; genuine multi-IP systems. Open items:
  address-map generation, clock domains, TCL templating.
- Retrofit poly/histogram to the hierarchical model for consistency (cheap, once proven).
- The memory-modeling unification and the class-level-`PortArray` parameterization (both flagged in
  `component.md`).
