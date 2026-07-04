# Plan — rowwise_fir free-running integration (Waveflow)

## Why
The free-running streaming FIR is **validated** (sandbox `fir_freerun_*`: 704 cyc/job, 2.08× the
control-driven sequential kernel, bit-exact, clean restart, per-job errors — all cosim gates pass).
Integrate it into Waveflow so the **generated** kernel reproduces the sandbox and the **sim** models the
streaming pipeline (3 persistent processes, cross-job overlap) calibrated to the freerun cosim.

**Supersedes** the control-driven Phase 2 in `fir-cleanup.md` §1 and the matrix-LT block kernel.

## Disposition of prior work (do this explicitly)
- **Discard** the uncommitted control-driven 2a (`fir.py`/`fir_sim.py` `on_start` restructure) — superseded.
  Base the new `fir.py` on **`main`'s committed version** (which already has the 3 persistent processes +
  AXI-stream control), not the 2a working copy.
- **Keep** `hwgen.py`'s m_axi-hook-arg branch (uncommitted) — the free-running hook takes `gmem`, so it's
  needed; commit it with Stage A.
- **Retire** the static `fir_top.cpp` / `fir.hpp` / `fir_tb.cpp` / per-row `fir_dataflow.tpp`
  (`fir_accel_core`) — the top is generated; `fir_pipeline.tpp` is the hook.
- **Re-derive** the timing artifacts: the matrix-LT `FIRTiming` (row_depth / master equation) + the old
  `results/{cosim_grid,fir_calibration}.json` + the figure are for the per-row block model — the streaming
  pipeline has a different timing story (per-job period + overlap). New calibration in Stage B.
- **Untrack `gen/`** at the end (`git rm --cached gen/` + ignore) — it's generated output now (like shared_mem).

## Design
- **Exec model = FREE-RUNNING.** No `VitisRegMapMMIFSlave` ⇒ `extract_kernel` picks **`run_proc`** ⇒
  `kernel_signature` emits **`ap_ctrl_hs`** (regmap-less + m_axi present, `hwgen.py:758`). Errors ride the
  **response stream** (no status regmap). Matches the sandbox exactly.
- **`fir.py`:** ports `s_in` (`StreamIFSlave`) / `m_out` (`StreamIFMaster`) / `m_mem` (`MMIFMaster`).
  `run_proc` = `yield from self.fir_pipeline(self.s_in, self.m_out, self.m_mem)` (the codegen marker).
  `fir_pipeline` = `@synthesizable(impl_file="fir_pipeline.tpp")`; **its sim body spawns the three
  PERSISTENT processes** (`load`/`compute`/`store`) over job-sized `transaction_queue`s, processes a batch
  until `END`, and models the **cross-job overlap** (load(N+1) ∥ store(N)). Schemas: `FIRCmd`/`FIRResp`,
  `FIROp`/`FIRError` (reuse / adapt from the current `fir.py`).
- **`fir_pipeline.tpp`:** the validated sandbox kernel — `load`/`compute`/`store` `while(!done)` stages in a
  `#pragma HLS DATAFLOW` region, shift-register FIR (II=1), `ap_uint<MEM_DW>*` gmem via
  `read_array_slice`/`write_array_slice`, `END`-sentinel drain, per-job status on the response stream —
  adapted to the hook signature the generated top calls.
  - **FIX the sandbox's command handling on the way in** (the sandbox hand-rolls it as a de-risk shortcut;
    do NOT re-touch the committed sandbox — fix it here):
    - **Command read from `s_in`** uses the **built-in `FIRCmd` deserialize** (`read_axi4_stream<32>`), NOT
      manual `c.op = (..)s_in.read();` casts (the §2a rule — applies in the `.tpp` now since the free-running
      command read lives in `load`, not an extracted `on_start`).
    - **Internal ctrl stream = a fixed-width (32-bit) serialized channel** carrying a **reduced `FirMeta`
      schema** (only `n_rows`/`n_cols`/`y_off`/`tx_id`/`status` — the fields `compute`/`store` need; NOT the
      full `Cmd` with its load-only `x_off`/`h_off`) via the **built-in serializers** — NOT a wide
      `hls::stream<Cmd>` struct FIFO. Uniform 32-bit-word + serializer basis, genuinely narrow.
- **`fir_build.py`:** gen-include (the `FIRCmd`/`FIRResp` schema headers + `Float32` array-utils) +
  `HlsCodegenStep(comp_class=FIRAccel, impl_dir=".")`, mirroring `poly_build.py`. The generated top is the
  `ap_ctrl_hs` wrapper calling `fir_pipeline`.
- **`fir_sim.py`:** host streams a **batch** of commands + `END`, asserts `ap_start`, drains the per-job
  responses (reads `status` from each — no regmap).

## Stage A — kernel + codegen (RTL bit-exact) — HARD CHECKPOINT ✅ DONE (2026-06-26)
**RESULT:** all 5 cosim scenarios bit-exact — single **1100** cyc, two **1804**, three **2508**
(steady **704 cyc/job** = sandbox freerun parity), clean (varying), error (bad-size flagged + ap_start
restart). Hook = `fir_pipeline_impl.tpp` (FixedBeat command read; 32-bit serialized `FirMeta` ctrl
channel; framework `read/write_array_slice` over m_axi). Committed `be417ea`; functional sim golden
bit-exact. **Key finding:** the earlier two-job "deadlock" was NOT a kernel bug — it was a symptom of
the `array_utils` slice burst bug (the scalar `write_array_slice` serialized to II=16 + dangling-m_axi),
fixed in commit **`3c1feeb`** (tag-dispatch slice; see [[reference-arrayutils-slice-codegen-gotchas]]).
Both commits pushed to `origin/main`.

1. **`fir_pipeline.tpp`** ported from `sandbox/fir_freerun_sandbox.cpp` (the DATAFLOW load/compute/store +
   shift-register + slice serdes + END drain + per-job status), adapted to the hook signature.
2. **`fir.py`** — ports + `run_proc` → `fir_pipeline` hook (sim body spawns the 3 persistent processes, but
   for Stage A a minimal/placeholder timing is fine — the RTL gate doesn't depend on the sim timing model).
3. **`fir_build.py`** — gen-include + `HlsCodegenStep`; **retire** the static `fir_top.cpp`/`fir.hpp`/
   `fir_tb.cpp`/per-row `fir_dataflow.tpp`.
4. **Confirm the generated top** = the sandbox shape: `ap_ctrl_hs` + `axis s_in/m_out` +
   `m_axi ap_uint<MEM_DW>* gmem` + a single call to `fir_pipeline`. (Reuses the `hwgen.py` m_axi-hook-arg
   branch — confirm it lowers the `m_mem` hook arg.)
- **Gate A:** csim + cosim **bit-exact** (single / multi / clean-varying / error / restart) — reproduces
  the sandbox; full non-vitis suite = `main`'s failure set; `ruff` clean. **Then untrack `gen/`.** Commit
  Stage A (incl. the `hwgen.py` branch). **Report and stop if gates fail.**

## Stage B — sim timing model (OCCUPANCY-based, near-fit-free) — ✅ DONE (2026-06-26)
**KEY PIVOT (user steer): do NOT fit end-to-end timing — too many confounds (contention, stall).
Model the COMPONENTS by their bus OCCUPANCY (deterministic beats), let the sim COMPOSE them, and
validate components match; the end-to-end is emergent.** This replaced an initial `[trips, n_rows]`
end-to-end period/latency fit (fragile: the *span* start→end inflates with interleaved beats + stalls
so the "regime flips" with size — a measurement artifact).

**THE MODEL (matrix-LT philosophy [[project-matrix-lt-fir-build]], adapted):**
- **bus occupancy = transfer beats == nwords** — confirmed EXACT for all 11 sweep points (load =
  `read_words` = n_rows·n_cols+T, store = `write_words` = trips). *Zero fit*; this is the component
  the user said to measure (occupancy, NOT span).
- **compute II=1** — `n_rows·n_cols` input samples. Zero fit.
- **FIR serializes load+store on the shared `gmem` bundle** (VCD <10% burst overlap) → modeled by
  a SimPy `Resource(cap=1)` (`fir.py::_bus_xfer`), so per job the bus moves `read_words+write_words`
  beats → the period *emerges* from occupancy, not a fitted curve. **KEY FINDING (duplex toy
  `sandbox/duplex_toy/`):** one `gmem` bundle is **FULL-duplex** — read+write cosim ≈ max (1063 vs
  read-only 1055), NOT sum (2109), for BOTH a single process (mode 2) AND two DATAFLOW processes
  (mode 3 = 1068). So the FIR's serialization is a **dataflow-dynamics** effect (load/store bursts
  don't coincide in time), NOT a bus limit → **~2× throughput headroom** if load∥store overlap.
  Confirms [[project-axi-fullduplex-fir-finding]] (full-duplex) and REFUTES the "half-duplex bundle"
  framing; the `Resource` is a phenomenological model of the measured non-overlap.
- **single calibrated residual `beta`** = m_axi sustained cyc/beat (the Vitis random-stall
  efficiency) — fit from period-vs-occupancy: **β≈1.437, bus_job≈20** (period = β·occ + bus_job).
  `pipe_fill≈88` (command-in + DATAFLOW fill/drain, a per-job LEAD-IN latency — spawned as a parallel
  delay `_delayed_put`, NOT a serial throughput cost; that was the bug that made tiny-job period 2×).

**RESULTS (emergent sim vs cosim, held-out 2×96):** occupancy EXACT (all 11). Period: **≤3.5% for
the throughput regime** (1×256 1.0%, 4×64 3.5%, 4×128 1.4%, 4×256 0.3%); ~12–13% mid (4×32, 2×96
holdout); ~28% at the degenerate tiny 1×16 (fixed-overhead + stall noise dominate — honest residual,
the freerun-notes caveat). Latency ≤11% (holdout 4.5%). Golden bit-exact; ruff clean.

**KEY IMPLEMENTATION NOTES (fir.py sim):**
- 3 persistent stages (`_load`/`_compute`/`_store`) + unbounded queues; dispatcher kicks `load`
  non-blocking → cross-job overlap. `pipe_fill` is a *parallel* spawned lead-in (latency, not
  throughput). The functional `read/write_slice` bus time is SUBSUMED into the bus-hold (`_bus_xfer`
  tops up to `occupancy·β` under the resource lock) — `fir_sim.py` zeroes xbar/mem latency so
  `FIRTiming` is the sole timing source. Measure period from a MID (contended) store-end spacing, not
  the last (drain) spacing; L1 from a single-job run (no inter-job store contention).
- `FIRTiming` baked defaults ≈ fitted; `from_calibration` reads `results/fir_calibration.json`
  `model_params` (β/bus_job/compute_beat/pipe_fill), falls back to defaults if absent/stale-schema.

**DONE:** `fir_sweep.py` (cosim grid → `results/cosim_sweep.json`, occupancy+spans+period),
`fir_calibrate.py` (assert beats==nwords; fit β; emergent Gate B), `fir.py`/`fir_sim.py` occupancy
sim, `fir_figures.py` (occupancy story: parity + period-vs-occupancy, `--check` byte-identical),
retired stale `fir_validate.py` + matrix-LT `results/{cosim_grid,cosim_holdout_ncol,fir_calibration_
results.md,timeline_single}`. `sandbox/duplex_toy/` isolation test added.
**REMAINING:** confirm duplex-toy verdict, suite = baseline, commit; (later) retire `sandbox/`.

## Validation summary
- Generated kernel **bit-exact** in cosim (Stage A); sim **calibrated** to the streaming cosim (Stage B).
- The **inter-job overlap** (the whole point) is preserved end-to-end: the generated kernel overlaps jobs
  (cosim), and the sim models it (the persistent processes + job-sized queues).
- `gen/` untracked; static `fir_top.cpp`/per-row `.tpp` retired; non-vitis suite = baseline; `ruff` clean.

## Follow-up (build correctness — after Stage A)
**Stale Vitis project ⇒ `-reset`/wipe the solution.** Stage A hit a stale build: cosim measured the OLD
`fir_accel_core` (from a reused `gen/fir_gen_proj/`) because the regenerate updated the source but the
build reused the prior Vitis solution. This is a **framework** issue, not FIR-specific — the BuildDag's
mtime staleness can't see stale RTL *inside* a Vitis project: `_path_mtime(dir)=max(contained files)` so a
churning `*_proj/` ~always reads "fresh", and Vitis's own `open_project` reuse lives *below* the DAG.
**Fix (decided) — generalize to a `BuildStep.clean()` protocol (better than a one-off `-reset`):**
1. **`BuildStep.clean(config)`** — default removes the step's `produces`; the csynth step **overrides** it
   to aggressively `rmtree` the Vitis `*_proj/` scratch dir.
2. **Auto-clean-before-rerun** — the DAG calls `step.clean()` before re-running a stale step, so every
   rebuild starts fresh (Vitis can't reuse a prior solution — fixes the "rebuild fresh" half structurally).
3. **Artifact vs scratch (fixes the *detection* half)** — the csynth step's *tracked* `produces` must be a
   **deterministic, write-once-per-success marker** (a report JSON / `synth.ok`), NOT the churning
   `*_proj/`. Reason: `_path_mtime(dir)=max(contained files)`, so a tool-rewritten project dir ~always
   reads "fresh" and defeats the `consumed-newer-than-produced` check. Track a clean marker; demote the
   project to scratch that `clean()` wipes.
4. **`--clean [step]`** — the **dual of `--through step`**: `--through step` builds `step` + its **upstream**
   closure (ancestors/deps); **`--clean step` cleans `step` + its **downstream** closure** (descendants that
   *consume* its output) — "invalidate from here down", computed from the DAG's `consumes`/`produces` edges,
   calling `clean()` on each (include `step` itself so it rebuilds). No `step` ⇒ clean **all** (`make
   distclean`). Robust because it does NOT rely on mtime propagation (the thing with the blind spots above).
One fix, all examples (poly/hist/vmac/fir). Read the shared csynth/cosim step + generated `run.tcl` first
to place the override + the marker correctly. The manual `rm gen/` clean-rebuild is this done by hand.

## Follow-up (framework — DATAFLOW-safe AXIS deserializer)
**`read_axi4_stream`'s TLAST early-return deadlocks a free-running DATAFLOW process.** Confirmed in Stage A
by isolation: the committed sandbox (straight-line `s_in.read()`s) cosims at 1086; the integration kernel
(identical except `FIRCmd.read_axi4_stream<32>` with 23 TLAST early-return branches in `fir_load`'s
`while(!done)`) **deadlocks** (csim passes — token-balanced; the failure is RTL-handshake inside
`#pragma HLS DATAFLOW`, surfacing only in cosim). §2a's *mechanism* (built-in `read_axi4_stream`) was fine
for the *sequential* `on_start` but does NOT transfer to a free-running DATAFLOW process; the *intent*
(schema-driven, no hand-rolled field casts) still holds. **Stage-A fix (kernel-local):** `fir_load` reads a
**fixed-beat** 7-word buffer (no TLAST branch) + `FIRCmd.deserialize(words)` (schema deserialize-from-words);
keep the narrow serialized `FirMeta` ctrl + the `m_out` write (fixed-count, not implicated). **Framework fix
(this follow-up) — `read_axi4_stream<W, FixedBeat>` template param (decided design):** the deadlock is the
data-dependent *early-return* (variable reads / branchy exit) breaking DATAFLOW *liveness*, NOT the TLAST
*check* itself. So the `FixedBeat=true` mode: **read a compile-time-constant N beats unconditionally** (no
branch → regular channel access → DATAFLOW-safe) and **fold the TLAST validation into a *data* flag** —
capture each beat's `last`, then after the loop compute `framing_ok = last[N-1] && !any(last[0..N-2])`,
returned in the `tlast_status` (rides the per-job `status`/response — malformed cmd → error status, pipeline
keeps flowing). Keeps §2a's framing validation with zero data-dependent control flow. `FixedBeat=false`
(default) = current early-return (no change for poly/sequential). Trade-off: fixed-beat assumes exactly-N-word
packets — it *detects* a framing anomaly but can't *recover* from a truncated packet (already over-read);
fine (a short command is a host bug, reported via status; mid-stream resync isn't practical in free-running).
**Once it lands, the `.tpp` reverts to a single `read_axi4_stream<W, FixedBeat>(s_in, tl)`** — schema-driven,
§2a-clean, DATAFLOW-safe. Benefits VMAC's free-running path. Refined §2a rule: *schema-driven but DATAFLOW-safe.*

## Out of scope
- VMAC's double-buffer (`stream_of_blocks`) hand-off — the *next* example (see `plans/example_sequence.md`).
- The docs/example walkthrough rewrite (the `docs/examples/rowwise_fir/` pages describe the old block model)
  — a follow-up docs pass after the code lands.

## Follow-up (ROOT CAUSE FOUND 2026-07-01 — the FIR forfeits ~1.8–2.3× full-duplex throughput)
Isolation ladder (`examples/rowwise_fir/sandbox/{compute_iso,loadstore_iso,lcs_iso,fir_skel,halfpipe}`)
pinned the FIR's period≈occupancy (read+write ADD, ~704 @4×64) vs the achievable `max(read,write)` (~306):
- Rung1 compute alone II=1.000 (L0=56, per-row L1≈0). Rung2 load‖store overlaps (~max). Rung3
  load→passthrough→store overlaps at EVERY depth (256..2048). Rung4 REAL FIR compute **direct
  gmem↔FIFO = 306 OVERLAP**, but the **2-pass `read_array_slice` buffer (gmem↔cb↔FIFO) = 534
  SERIALIZED** (→ real 704 with the h-read + FIRCmd deserialize on top).
- **Root cause:** the hook (`fir_pipeline_impl.tpp`) uses `read_array_slice`/`write_array_slice` — the
  "resident" path — where `docs/guide/vectorization/hls/raw.md` "The lane loop" is the canonical
  **throughput** path (stream `gmem↔FIFO` direct, no buffer). A hook-authoring mistake, not a primitive
  bug. See [[project-fir-slice-vs-laneloop-rootcause]].

**FIX (one step at a time):**
1. **LW=1:** rewrite hook load/store as the lane loop, direct `gmem↔FIFO`; `static_assert(lane_capacity
   ==1)` so LW>1 fails loudly (unsupported). Keep h-read as `read_array_slice` (genuinely resident).
   Cosim bit-exact + confirm period drops ~704→~306 (recovers ~1.8×).
2. **LW>1 (deferred vectorization target):** `s[LW][T]` shift register (window T+LW-1), UNROLL inner T
   AND outer LW, `ARRAY_PARTITION complete` on sr+h → **LW samples/cycle = LW·T mults/cycle**. Edges
   (per-row flush + T-1 warmup + n_cols%LW) are the fiddly part. Bus-width→throughput made concrete.
3. **Re-calibrate Stage B — DEFERRED (2026-07-01), needs a streaming re-model, not a tweak.** Re-ran
   the sweep on the fixed kernel: clean spans (``load_span≈read+22``, ``store_span≈write``, β≈**1.0**,
   the stall gone), BUT the occupancy law is broken — the overlap is now **partial + size-dependent**
   (small/1-row jobs ≈ serialize ``P≈load+store``; large 4-row overlap ``P→max``; ``P/occ`` slides
   2.30→0.59). AND the fix enabled **within-job streaming** (load/compute/store overlap within a job),
   so the single-job latency is no longer the serial stage sum. A pragmatic full-duplex channel-split
   on the current *atomic-stage* sim gives ~186% latency error → the fixed kernel needs the
   **double-buffered / streaming-timing** model (per-element or anchored overlap, the ``pymodel.md``
   design Stage B skipped for the occupancy shortcut — which worked only because the buffered kernel
   serialized). ``fir.py`` marked STALE (banner); golden unaffected; new sweep data was reverted (regen
   via ``fir_sweep.py`` when the re-model happens). A focused future task.
