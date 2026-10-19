# Plan: characterize the systolic matrix multiply, then redesign it for an outer loop

**Status:** Not started (drafted 2026-10-19; revised 2026-10-19 with the user: stream-fed timing,
trimmed calibration, and the scratchpad / outer-loop stage).
**Complexity:** Part A (Stages 0-5) L2-L3: a docs move, an architecture page, a calibration harness
that probably needs one framework extension (events on stream-of-blocks channels), and a short RTL
calibration. Part B (Stage 6) L3: a new command set for the unit, a wrapper with memory adaptors, and
their calibration. Nothing irreversible.
**Plan file:** `plans/systolic_characterization.md` · **Lessons:** `plans/systolic_characterization_lessons.md`
(the doer creates it at the first lesson).

> **For the agent executing this plan:** read *Rules for the doer* first. Paths are repo-relative. The
> plan states what is known (with its evidence) apart from what is suspected; never promote a suspicion
> to a fact without the measurement that settles it.

## Rules for the doer

1. **Another agent works in the main checkout** (`C:\Users\sdran\Documents\repos\pysilicon`, branch
   `next-docs-examples`, with uncommitted work). Never edit, stash, commit or switch it. Work in a
   **separate worktree** on branch `systolic-char`, created from `main`. Set `PYTHONPATH` to the
   worktree root: the venv's editable install maps to the main checkout (memory
   `reference-worktree-agents-pythonpath`). Run Python as `../pysilicon-venv/Scripts/python.exe`.
2. One stage at a time (§ Stages). A stage is done when its gate is verified by the stated command;
   paste the command and its result into the stage's record in this file.
3. Root-cause failures from the error, logs and traces. Up to **two hypotheses**, test each, then stop
   and report.
4. **Pause for the user at the end of each stage and on any surprise**, with a two-line status.
5. **A measured number names its tool version.** This machine has Vitis HLS / Vivado **2025.1**; every
   number already on the systolic page is **2024.1**. Never mix them in one table without the version on
   each row.
6. **Pre-register before measuring** wherever a fit is scored (Stage 6's calibration): the
   configurations and job shapes of the fit and held-out sets are committed before any run, and the
   held-out set is scored once.
7. Never merge or push unless the user asks. One PR per part, many commits (memory
   `feedback-single-pr-multi-commit`).
8. Before building a tool the plan did not foresee, search the KB first (`waveflow kb search ...`) and
   propose it (memory `feedback-search-before-designing`).
9. No wall-clock measurements on this laptop: count cycles and events only (`CLAUDE.local.md`).
10. HLS bodies follow `docs/guide/vectorization/hls/loop_optimization.md` (straight-line loop per
    message; never pack words by hand -- use the generated array utilities).

## Goal

The user asked for:

1. Move `docs/guide/linalg/systolic.md` and `cg_vector.md` to `docs/guide/dsp/`.
2. In `docs/guide/dsp/systolic/`, a page describing the **precise operation** of the array: how it
   tiles `C`, how operands are loaded, how results are stored.
3. A **calibration harness** with the unit-harness apparatus (`harness(module)`, timed phases,
   `add_calib_steps`, `ModuleStore`; `docs/guide/flows/units*.md`) that **validates the operation and the
   pipelining**, with docs saying **where the timing is measured**.
4. A **timing calibration** that confirms the operation.
5. (Added 2026-10-19) A **redesign** in which the unit holds operands in an addressed local memory
   (`Load` / `MatMult` / `Store` commands), so one operand can be reused (the CG application) and a
   **wrapper with memory adaptors** can run an **outer tiling loop** over matrices larger than the unit.

**Order, as agreed:** characterize the current architecture first (Part A), kept small, since its
core compute, its tooling and its overlap diagnosis carry over; then the redesign (Part B), which gets
the full calibration. Part A spends nothing on what Part B replaces: no multi-configuration fit of the
message-level timing, no held-out scoring, pysim stays on `cost.core_interval`.

**The open question behind items 3-4** (raised 2026-10-18): why do transfer and compute not overlap?
The memory-fed RTL takes 720 cycles for the centre `8x8x32` job (2024.1), the fit's "core share" is
400, and the cost model's form is a *sum* of compute and transfer terms. The docs explain the sum by
the generated top's `m_axi` pointer FIFOs (`docs/guide/linalg/systolic.md:388`,
`waveflow/linalg/cost.py:26-28`). That explanation is **unverified and doubtful**: the FIFOs are 9 deep
(`tests/linalg/cpp/unit_bench_tasks.h:9-11`), which lets the reader run about four jobs ahead, and the
unit has `sob_depth = 2` ping-pong blocks between every task.

**The unit is stream-only**, so it is timed without any memory: its harness puts an `AxisMaster` on
`s_in` and an `AxisSlave` on `s_out`, with no `m_axi`, no pointer FIFOs, no memory model, no framers.
If the unit overlaps across jobs, its stream-fed steady interval for `8x8x32` should be near
`max(core ≈ 400, 163 words in, 131 words out) ≈ 400`; if it serializes internally, near 700.

## What is known (verified 2026-10-18/19, read from the sources)

| Fact | Evidence |
|---|---|
| The unit = `SystolicRx` -> `SystolicLoad` -> `SystolicCore` -> `SystolicStore`; Rx->Load and Rx->Store are streams, Load->Core and Core->Store are `hls::stream_of_blocks` | `docs/guide/linalg/systolic.md:104-127`, `waveflow/linalg/systolic.py` |
| The protocol: request = `LinalgHeader(tag, op, m, k, n, length, nfollow=0)` \| `A` \| `B` (three `TLAST` bursts); reply = header with status \| `C` (header only if refused). Status rides in the header; errors are not fatal (drained by `length`) | `docs/guide/linalg/index.md:88-117`, `systolic.md:129-149` |
| The core's blocks hold whole matrices: `Mmax x Kmax`, `Kmax x Nmax`, `Mmax x Nmax` in lane groups | `waveflow/build/systolic_core_task.h:69-76` |
| The core copies `X` into `xs_r/xs_i[MMAX][KG][L]` once per job (kept over `nb` matrices `B`, with `roff` pre-skew offsets), and each `B` into `br/bi[KMAX][NT][C]`, banked by array column | `systolic_core_task.h:106-170, 186-192` |
| Inside one core firing the phases are sequential: copy `X`, then per `B`: copy `B`, then per tile `(rt, ct)`: clear the accumulators, sweep `t < k+R+C-2`, round and write `R*C/L` groups into the `C` block | `systolic_core_task.h:118-275` |
| The core is output-stationary at the array level: each PE accumulates its entry of `C` over `k` at `acc_t` width and rounds once | `systolic_core_task.h:198-274` |
| The core takes a `read_lock` on a whole block before using it, so within one job its compute cannot start before the block is full | `systolic_core_task.h:118, 155` |
| The unit never touches memory; the RTL tests feed it from memory through an in-band `MemRStream` / `MemWStream` with two test-only framers | `tests/linalg/_unit_bench.py:1-15` |
| The memory-fed writer must fire as often as the reader (an empty write per reply) or the RTL hangs after 8 jobs (pointer FIFO depth 9) | `tests/linalg/cpp/unit_bench_tasks.h:8-11`, `plans/mimo_cg/mimo_cg_lessons.md:410-413` |
| pysim (core share, transfers overlapped) vs memory-fed RTL, 2024.1: `8x8x32` 400 vs 720; `Aᴴ` 400 vs 801; `4x8x8` 103 vs 181; `8x2x16` 180 vs 282 | `plans/mimo_cg/mimo_cg_paper_sims.md:1109` |
| The stream-fed unit at RTL has never been run ("untested" in the page's Limits) | `docs/guide/linalg/systolic.md:466-475` |
| The harness has stand-ins for streams, credit streams and memory-master ports; **none for stream-of-blocks** | `docs/guide/flows/units_harness.md:38-52` |
| A timed phase's events are first/last beats **at a port of the generated kernel** | `docs/guide/timing_model/timed_phases.md:42-56, 71-80` |
| Vitis HLS csynth and XSI work for `xczu48dr` here; Vivado `synth_design` does not (licence). Timing needs no Vivado synthesis | memory `reference-vivado-license-no-rfsoc-synth` |
| `hls::stream_of_blocks` ping-pongs between free-running `hls::task`s at RTL (measured 2025.1 on the interleaver); a task must not mix `m_axi` and block access | memory `reference-hls-stream-of-blocks-pingpong` |

**Suspected, not verified** (candidate causes of the missing overlap): in the unit, a blocking
handshake (the receiver or loader waiting on the core's next command, or a shallow command channel);
on the memory side, the AXI-MM model serving one transaction at a time, `MemRStream` / `MemWStream`
round trips per firing; and the fit's split between "core" and "transfer" terms being an artefact of
correlated features. Stream-fed timing (Stage 0) separates the first group from the second.

## Decisions for the user (asked at the end of Stage 0)

- **D1 -- the shared linalg page.** `docs/guide/linalg/index.md` holds what both components share
  (formats, lanes, messages, build, cost data). Recommended: move it too, as
  `docs/guide/dsp/linalg_common.md` ("Linear-algebra conventions"), and delete the `linalg` section.
  Alternative: keep it where it is, with links to the moved pages. The Python package stays
  `waveflow/linalg/` either way.
- **D2 -- how the pipelining is observed.** `SystolicLoad`, `SystolicCore`, `SystolicStore` have
  stream-of-blocks ports, so none can be harnessed alone, and their phases cannot be stated at kernel
  ports.
  - **(a) Recommended: stream-of-blocks events.** Extend the apparatus so a block channel's
    write-acquire, write-release, read-acquire and read-release are events, logged by pysim's sob
    primitive and read off the composite's RTL trace. Leaf phases are then declared and fitted inside
    the unit's harness. Reusable: the CG unit, the interleaver, and Stage 6's scratchpad unit.
  - **(b) Fallback: boundary only.** Time the unit at `s_in` / `s_out`, plus a diagnostic VCD read of
    the internal channels that is not part of a fit.
- **D3 -- where the 2025.1 numbers live.** Recommended: a `ModuleStore` under the test or example
  directory, keyed by identity, naming 2025.1. The packaged `xczu48dr_250mhz_vitis2024_1` platform is
  not touched.
- **D4 -- `cg_vector`.** Recommended: moved only. No new page, no calibration in this plan.
- **D5 -- the branch point.** If Stage 0 finds the stream-fed unit already overlaps (interval near
  400), the current architecture has no timing defect; recommended then: do Stage 1, a short Stage 2,
  skip Stages 3-5, and go to Stage 6.

# Part A -- the current architecture

### Stage 0 -- readiness and the three facts (no code changes)

1. Create the worktree and branch (rule 1). Confirm `-m "not vitis and not xsi"`
   `tests/linalg tests/docs` passes there (memory `project-test-baseline-failures`: only the KB
   index-timing test is expected to fail).
2. **Baseline at 2025.1.** `pytest -m xsi -rs tests/linalg/test_systolic_unit.py`; record the reply
   cycles beside the 2024.1 record (`mimo_cg_paper_sims.md:1101`: 789, 1,097, ...). A difference is
   expected and recorded, not a failure.
3. **Fact A -- the unit stream-fed.** `harness(SystolicUnit(...))` builds an `HwSystem` with a
   `StreamDriver` on `s_in` and a `StreamSink` on `s_out`; `add_system_steps` runs it at RTL (csynth +
   XSI, no memory). Centre configuration, one job, bit-exact.
4. **Fact B -- can a block channel be traced?** In the csynth output, find the RTL signals of each
   stream-of-blocks channel's acquire/release handshakes; name them; check that the VCD shows them
   toggling once per block. Check UG1399 for whether a stream-of-blocks may be a top-level argument (if
   so, a block stand-in for a leaf is a third option for D2).
5. **Fact C -- the overlap, first look.** Fact A's harness with 4 back-to-back `8x8x32` jobs: the steady
   interval at `s_out` (reply to reply). Near 400: the unit overlaps, and the memory-fed sum comes from
   the memory side. Near 700: the unit serializes; Fact B's signals show where.

**Gate:** the record has the 2025.1 baseline and Facts A-C with evidence (commands, signal names,
cycle numbers); D1-D5 answered by the user. **Pause.**

#### Record: Stage 0 (2026-10-19) -- the unit serializes on a 2-deep FIFO, not on memory

All numbers: Vitis HLS / Vivado xsim **2025.1**, `xczu48dr-ffvg1517-2-e`, 4 ns, centre configuration
(`Mmax 8, Kmax 8, Nmax 32, R 4, C 8, L 4, form 4`, formats `(12,3) (12,4) (12,5)`, 64-bit words).
Worktree `.claude/worktrees/systolic-char`, branch `systolic-char` from `main` @ `cc3858b8`. Scratch
script (not in the repo): `<scratchpad>/fact_a/unit_harness_run.py`; builds in `C:\wfsc\`.

1. **Readiness.** `pytest -m "not vitis and not xsi" tests/linalg tests/docs` in the worktree: pass
   (3 quarantined, as `tests/conftest.py` says). `import waveflow` resolves to the worktree with
   `PYTHONPATH` set.
2. **Windows path length breaks csynth silently.** Built under the scratchpad (a long path), csynth of
   both the bench and the unit failed with only `WAVEFLOW_ERROR: csynth` / `Synthesis failed.`; the flow
   log shows `syn_report` failing on
   `.../.autopilot/db/systolic_rx_task_64_8_8_32_4_4_8_1556958752_Pipeline_FWD.verbose...` -- **266
   characters**, past `MAX_PATH`. From `C:\wfsc\` both pass. Build these tops from a short path.
3. **Baseline, memory-fed (`pytest -m xsi tests/linalg/test_systolic_unit.py --basetemp=C:/wfsc/base`):
   4 passed** (505 s), bit-exact. Centre reply cycles: 759, 1,088, 1,139, 1,233, 1,484, 1,535, 1,894,
   1,947, 1,980, 2,237 (2024.1: 789, 1,097, 1,136, 1,235, 1,478, 1,517, 1,882, 1,921, 1,960, 2,205).
   **Defect:** the test writes `"tool": "Vitis HLS / Vivado xsim 2024.1"` into `job_cycles.json`
   whatever ran (`tests/linalg/test_systolic_unit.py:215`).
4. **Fact A -- the unit in its own harness: works, after four fixes** (all done in the scratch only):
   - `harness()` rebuilds a module from its `HwParam`s (`waveflow/hw/harness.py:38-53`); the unit's
     formats `a`, `b`, `c` are plain fields (`waveflow/linalg/systolic.py:705-707`), so they are
     dropped. Scratch: a subclass with the formats as defaults.
   - `s_in` / `s_out` are plain `StreamIFSlave` / `StreamIFMaster` (`systolic.py:496, 650`), so the
     unit's own top declares `ap_uint<64>`, but the bodies take `framed_word<64>`. Scratch: retyped to
     `FramedStreamIF*`.
   - With framed boundary ports the top declares `ap_axis`, and the bodies are not templated on the
     boundary word type (`KernelTask.framed`, `waveflow/hw/mem_stream.py:200-210`). Scratch: two
     one-word adapter tasks (`axis_to_framed`, `framed_to_axis`) spliced into the generated top.
   - The system top must not share the DUT's name (`top="systolic_unit_sys"`), or xelab reports an
     illegal recursive instantiation.
   Then `codegen, scenario, csynth, pysim, system_rtl, system_xsi, compare`: **all ok**, compare
   (pysim vs RTL) no mismatches, 4 jobs `8x8x32` bit-exact.
5. **Fact B -- the block channels are traceable.** UG1399 (2025.2, *Data-driven Task-level
   Parallelism*): a stream-of-blocks cannot be a top-level interface, so a leaf with block ports cannot
   be harnessed alone (the third D2 option is out). In the csynth RTL each block channel is a
   `<ch>_RAM_AUTO_1R1W` ping-pong (`BufferCount = 2`) instanced in the top scope; the level-1 trace
   dumper records its handshake wires: `<ch>_i_full_n`, `<ch>_t_empty_n`, the writer's release
   `<writer>_U0_<ch>_blk_write`, the reader's release `<reader>_U0_<ch>_blk_read`. Observed once per
   block per job. Note: its `full_n` drops after **one** written block (`count == BufferCount - 2`), so
   the writer cannot complete a second block before the reader releases the first.
6. **Fact C -- stream-fed, the unit serializes: steady interval 821 cycles.** Per job: input 163 words,
   core 388 (`ccmd_read` 202 -> `c_blk` release 590 in the trace), `C` out 128 words in **254 cycles
   (II 2)**; `163 + 400 + 254 ≈ 821`. Job 1's header and 4 words of `A` were accepted at 182-188, then
   nothing until 841, right after job 0's last `C` word (837).
   **Root cause (traced):** the store keeps its firing open for the whole job (its reply header's
   third word waits until `C` is ready: words at 18, 20, **578**), and the receiver -> store channel
   `reply` is a **2-deep** FIFO while a reply header is **3 words** at 64 bits. For job 1 the receiver
   blocks on the header's third word, so it never forwards job 1's payload, and the loader starves until
   the store finishes job 0. The block buffers do not bind: the core copies `X` and `B` into its own
   arrays and releases both blocks early (224, 292).
   **Confirmed by one change:** `#pragma HLS STREAM variable=reply depth=8` in the scratch top, nothing
   else: **steady interval 390** (= the core's share), input of job *n+1* during the compute of job
   *n*, output overlapped; bit-exact, compare ok.
7. **Consequence for the memory-fed number.** The same 2-deep `reply` channel sits inside
   `UnitBench`, so the memory-fed "sum of compute and transfer" (720 at 2024.1, about 759 here) is most
   likely this serialization too, not the `m_axi` pointer FIFOs. Not yet verified: a memory-fed run
   with the deeper channel settles it (Stage 4).
8. **Two more things the trace shows**, for Stage 2: the store writes `C` at one word per 2 cycles,
   and the core's `B` copy and sweep run inside one firing (`ccmd_read` -> `b` release 90 cycles).

**For the user:** D1-D5 as listed, and **D6 -- fix the `reply` depth in Part A?** The fix is one
channel depth (at least `header_words`, e.g. from `request_words`' header size), declared where the unit
builds its channels. It changes the RTL timing of every recorded configuration, so the packaged
2024.1 cycle model no longer describes the fixed unit. Recommended: fix it in Part A (Stage 3), and
re-measure the memory-fed bench to close item 7; keep the 2024.1 platform as the record of the old
unit. D5's branch does not apply: the unit did not overlap as built.

### Stage 1 -- move the pages

1. `git mv docs/guide/linalg/systolic.md docs/guide/dsp/systolic/index.md`;
   `git mv docs/guide/linalg/cg_vector.md docs/guide/dsp/cg_vector.md`; the shared page per D1.
2. Front matter: `parent` / `grand_parent` / `has_children` (the systolic page gets children);
   `nav_order` within DSP Blocks after the SSR FFT. Check for nav-title collisions (memory
   `reference-docs-nav-title-collision`).
3. Fix every relative link inside the moved pages (they gain one `../`), and every link to them:
   `docs/examples/mimo_cg/index.md` (lines 20-22, 36, 170), `docs/overview/status.md:50`,
   `docs/overview/salsa.md:81`.
4. `docs/guide/dsp/index.md`: list the systolic array and the CG unit; update its summary (today "today
   the full-rate SSR FFT").
5. Tests and pointers: `tests/docs/test_documented_numbers.py:709-732, 895-900` (`_page("guide/linalg/...")`),
   `CLAUDE.md:74`, and any KB / MCP index that lists pages.
6. Right before committing, `git log main -- docs/guide/linalg` to check the other agent did not edit
   these pages meanwhile; rebase if it did.

**Gate:** `pytest -m "not vitis and not xsi" tests/docs` passes; a grep for `guide/linalg` over
`git ls-files` returns only plans; `waveflow kb search systolic` returns the new paths; `git diff -M
--stat` shows renames with only links and front matter changed. **Pause.**

### Stage 2 -- the architecture page `docs/guide/dsp/systolic/architecture.md`

Written from the bodies, every claim citing a body line, and **in two-level terms**: tiling from
off-chip memory into on-chip storage (level 1) and from on-chip storage into the array (level 2).
Today's unit has level 2 only: it loads whole matrices, so off-chip traffic is already minimal (each
element moves once), but only for matrices up to `Mmax`, `Kmax`, `Nmax`. Sections:

1. **The job and its blocks.** `SystolicCmd`; each task's work per firing; what each channel carries;
   what `nb` changes (a fixed special case of operand reuse: `X` resident over `nb` matrices `B`).
2. **The on-chip storage layout** -- precisely, since Stage 6's partition-aligned addressing must
   respect it: `xs` as rows of lane groups with the `roff` pre-skew, `B` in `C` column banks (array
   column `j` reads bank `j`), the `C` block's group order, and what each array partition costs in
   BRAM.
3. **Load.** How `SystolicLoad` unpacks message words into lane groups (`wf_load_matrix`), the
   transposing load for `MUL_AH` (`wf_load_matrix_transposed`), and the core's copy into its banks.
4. **Level-2 tiling.** Tile order (`rt` outer, `ct` inner); per tile: clear, the skewed sweep of
   `k + R + C - 2` steps, which value enters each edge PE at step `t`, the shift right / down, the exact
   accumulate; why `k` must be a multiple or divisor of `L`.
5. **Store.** The single rounding and the conjugation for `MUL_AH`; the `R*C/L` groups per tile;
   `SystolicStore`'s packing into reply words.
6. **The schedule.** Per task, the loops with trip counts and the II csynth reports (version named),
   and the **predicted** timeline of back-to-back jobs: which phases may overlap through the ping-pong
   blocks and which may not. Written **before** Stage 4 measures it.
7. **Level 1, not built.** The output-stationary outer loop and its traffic (`≈ 2·MNK/b` for a `b x b`
   on-chip `C` tile, `b ≈ √S`), with a pointer to Stage 6.
8. Figures: the array with its skew (TikZ -> SVG, memory `reference-docs-figure-tikz-workflow`), the
   storage layout, and the per-task timeline (predicted; Stage 4 adds the measured one).

**Gate:** every number comes from a body or a named csynth report; a snippet (or a test) checks the
trip counts the page quotes against `cost.message_features` for two shapes; docs tests pass; the user
reviews the page. **Pause.**

### Stage 3 -- the stream-fed calibration harness

Per D2. With (a):

1. **Framework: stream-of-blocks events.** pysim's sob primitive logs acquire / release per channel
   as transfer events; the trace reader maps them to the RTL handshake signals of Fact B; a timed phase
   may name a block channel's event. Tests on a small two-task sob design (not the systolic unit).
   Docs: a section in `docs/guide/timing_model/` and a row in `units_harness.md`'s table.
2. **Declared phases, keyed to the core's own phases, not the message:** `X` copy, `B` copy, tile sweep
   (features `k`, `R`, `C`), tile output (`R*C/L`), plus the receiver, loader and store phases at their
   block and stream events. Keyed this way the core's phases carry over to Stage 6's `Load` /
   `MatMult` / `Store` almost unchanged. Every phase states its features and whether it may be held.
3. **`SystolicUnit.workload(kind)`** for `"test"` (the shapes of `test_systolic_unit.py`, including
   rejected requests) and `"calib"`, and `calib_holdout`.
4. **Stream-fed harness only:** `harness(SystolicUnit)`. The memory-fed bench (`UnitBench`) is
   **optional**: run it in Stage 4 only if Fact C showed the unit overlaps stream-fed, to locate the
   ~320 extra cycles of the 2024.1 memory-fed number on the memory side.
5. `add_calib_steps` in a build script (location asked at the stage start), filing into a
   `ModuleStore` per D3.

With (b): items 3-5 only, the unit as one module, and a diagnostic VCD reader for the sob channels.

**Gate:** the harness runs in pysim and at RTL on `workload("test")`, bit-exact against the model,
every reply header as expected; `WANT_XSI_GATES` updated for the new RTL tests (memory
`project-xsi-staleness-hash`). **Pause.**

### Stage 4 -- the calibration run (short)

1. Two configurations: the centre (`Mmax 8, Kmax 8, Nmax 32, R 4, C 8, L 4, form 4, W 12, 64-bit
   words`) and one varying `R` and `C`. Job shapes: `MUL` and `MUL_AH`, `nb` 1 and 2, back to back and
   isolated, rejected requests between. Commit the list before running.
2. Run `add_calib_steps` on each: csynth (2025.1, `xczu48dr-ffvg1517-2-e`, 4 ns), XSI traced, fit,
   file.
3. **Confirm the operation:** the measured per-task timeline against Stage 2's prediction. Every
   disagreement is explained (and the page corrected) or recorded as open.
4. **Answer the overlap question** from the traces: does job *n+1*'s load overlap job *n*'s compute?
   If not, which handshake waits on which. If the memory-fed bench runs (Stage 3 item 4): is its
   reader ever held by a full pointer FIFO?
5. **One hostile run:** `add_robustness_steps` with a stalling sink on `s_out` (closes the
   "back-pressure untested" limit).

Not done here (Part B replaces it): a multi-configuration fit, held-out scoring, a change of pysim's
time source.

**Gate:** bit-exact everywhere; the timeline compared; the overlap question answered with a trace
figure; every number names 2025.1. **Pause.**

### Stage 5 -- the timing page and the corrections

1. `docs/guide/dsp/systolic/timing.md` (like `docs/guide/dsp/ssr_fft/timing.md`): **where the timing is
   measured** -- per phase, its two events, the port or block channel each sits on, the RTL signal the
   trace reads and the pysim primitive that logs it; the measured timeline beside the predicted one;
   the overlap answer.
2. The systolic index page: *Cycles* and *Limits* rewritten to what Stage 4 found; the pointer-FIFO
   sentence replaced by the measured cause, and the same in `waveflow/linalg/cost.py:26-28`. Stream-fed
   RTL and back-pressure come off the "untested" list.
3. `tests/docs/test_documented_numbers.py`: the new numbers checked against committed records (memory
   `reference-gitignore-drops-json`: `git check-ignore` every new JSON, `git add -f`).

**Gate:** `pytest -m "not vitis and not xsi"` passes (baseline failures excepted); `pytest -m xsi
tests/linalg` passes; every number on the new pages traces to a committed record. Part A's PR is
ready for the user. **Pause.**

# Part B -- the scratchpad unit and the outer loop

### Stage 6 -- addressed operands, accumulate, and a tiling wrapper with memory adaptors

**Why.** Two needs the current command cannot meet: reusing an operand across jobs (in the CG
application one operand stays the same over many products), and matrices larger than the unit. The
second needs a **level-1 outer loop**. The traffic-optimal form is output-stationary: a `C` tile held
on chip at full accumulator width while panels of `A` and `B` stream past it, with traffic
`≈ 2·MNK/b` for a `b x b` tile, `b ≈ √S` for `S` words of on-chip memory (the Hong-Kung bound, tight
constant `≈ 2·MNK/√S`, Smith et al. 2019). Holding three equal `R x R` blocks and reading and writing
partial `C` blocks costs `≈ 3·MNK/R`, and rounds `C` more than once unless the partials are stored
at `acc_t` width.

**The split.** The inner unit knows nothing about DRAM; a **wrapper** owns every address and every
loop index. The wrapper is a composite of a sequencer, `MemRStream` and `MemWStream`; the inner unit
stays stream-only. Leaves are calibrated, composites composed and checked against their RTL once --
the unit-harness practice.

```
s_cmd: Job(tx_id, M, N, K, a_base, b_base, c_base, strides) -> Sequencer
Sequencer -> MemRStream cmds (A / B panels)     MemRStream -> inner unit s_in
Sequencer -> inner cmds (in-band, ahead of the data each consumes)
inner s_out -> MemWStream (C tiles + their cmds)            -> Status(tx_id), one per Job
```

#### 6a -- the design (a page of decisions, reviewed before any code)

The inner unit's command is a `DataUnion` (user's sketch, 2026-10-19):

| Command | Fields | Does |
|---|---|---|
| `Load` | `dst` region, role (`A` or `B` layout), dimensions, transpose flag | lands an operand in the region's banking (rows for `A`, column banks for `B`); the `MUL_AH` transpose moves here |
| `MatMult` | `a`, `b`, `c` regions, mode (`CLEAR` or `ACC`) | sweeps the array over the regions; `C` regions hold **`acc_t`**, so partial sums over the reduction stay exact |
| `Store` | `c` region, dimensions | rounds and saturates **once**, conjugates for `Aᴴ`, streams `C` out |

Requirements the design must meet, each a decision record in this stage:

1. **Exactness.** Rounding only at `Store`, so a wrapped `M x N x K` product equals
   `matmul(...)` bit for bit, whatever the tile size. The bit-exact model stays the golden for the
   whole job.
2. **Partition-aligned regions.** Region addresses aligned with the memory partitions so loads and
   the sweep run at full rate; the layout follows Stage 2's storage section. Decide whether one memory
   serves both roles or `A` and `B` regions are separate memories. Account for BRAM18K vs UltraRAM
   (XCZU48DR: 2,160 BRAM18K, 80 UltraRAM; confirm against DS891 before quoting) and URAM's fixed 72-bit
   ports.
3. **A result used as an operand.** Whether a stored `C` (at `c_t`) may be loaded back as `A` or `B`
   on chip, and the format rule if so.
4. **Overlap.** At least two regions per operand, so the wrapper loads the next panels while the
   current ones compute. Simplest correct scheme: in-order execution with the wrapper alternating
   regions; plus a hazard check that refuses a `Load` into a region an issued `MatMult` still reads,
   returning a status instead of wrong data. The Stage 4 overlap finding decides what this must avoid.
5. **Edge tiles.** Zero padding by the wrapper (exact) with the zeros made by the memory adaptor, not
   read from DRAM; or masked partial tiles in the unit. Recommend padding unless the cost says
   otherwise.
6. **Protocol.** Move to the default command-response shape (`docs/guide/systems/command_response.md`):
   `tx_id` per command, a `Status` per command on its own stream, and decide whether errors become
   fatal. Inside the wrapper, every inner command travels in-band ahead of the data it consumes, so
   ordering holds by construction and not by two streams staying in step.
7. **The DRAM layout.** A `b x b` tile of a row-major matrix is `b` short bursts. Decide between a 2-D
   strided read in the memory adaptor (one command, `b` rows) and block-major storage in DRAM (one
   burst per tile; a host / firmware choice). Balance the reader's and the writer's firings by design
   (a fixed ratio of panel reads to tile stores), padding with empty writes only if unavoidable
   (the pointer-FIFO rule of `tests/linalg/cpp/unit_bench_tasks.h:8-11`).
8. **Bandwidth.** Per step a `b x b` tile reads `2b` words and does `b²` MACs: compute-bound when
   `b ≥ 2P/W` (`P` PEs, `W` words per cycle). Size `b` (the on-chip tile) well above the array, and
   state the resulting `S` per configuration.
9. **What happens to the current unit.** Keep `SystolicUnit` as is (the `mimo_cg` study and its
   calibrated platform depend on it) beside a new unit, or replace it. Recommend: keep, add new.

**Gate:** the decisions written into this plan (a record section) and approved by the user.
**Pause.**

#### 6b -- the inner unit

1. Bit-exact model of the command stream (a Python model of `Load` / `MatMult` / `Store` over regions,
   checked against `matmul` on random command sequences that tile larger products).
2. pysim module and HLS bodies (receiver, loader, core, store), reusing the current core's sweep.
3. Stream-only harness with `workload("test")`: random tilings, `ACC` chains, a hazard refused, every
   status. Timed phases per command, keyed as in Stage 3 item 2.

**Gate:** pysim and RTL (XSI, 2025.1) bit-exact against the model on `workload("test")`; csynth at
4 ns. **Pause.**

#### 6c -- the wrapper

1. The sequencer: one `Job` -> the output-stationary loop nest, as a straight-line loop per tile
   (rule 10), emitting memory commands and inner commands; the `Status(tx_id)` per `Job`.
2. The memory adaptors: `MemRStream` / `MemWStream` as they are, or with a 2-D strided read if 6a
   decided so (a framework change with its own tests).
3. The wrapped system: `HwSystem` with the wrapper as DUT and a memory, golden = `matmul` on the whole
   `M x N x K`.

**Gate:** pysim and RTL bit-exact on products several times larger than the on-chip storage in every
dimension, with edge tiles, both operations; the reader and the writer balanced (no stall after any
number of jobs). **Pause.**

#### 6d -- calibration and docs

1. **Pre-register** (rule 6): configurations (on-chip size, array, lanes, word width) and job shapes,
   fit and held-out, committed before any run.
2. Calibrate the inner unit in its harness (`add_calib_steps`), the memory adaptors from their records
   (`MemCalibStep`), and compose the wrapper's pysim from them; check it against the wrapper's RTL
   once. Score the held-out set once: the wrapped job's cycles, pysim against RTL (target mean ≤ 3%,
   worst ≤ 6%; a miss is reported, not tuned away).
3. Measure the overlap the design promised: the steady interval of a wrapped product against its
   bound `max(compute, traffic / W)`.
4. Docs: `docs/guide/dsp/systolic/` pages for the scratchpad unit and the wrapper (commands, regions,
   the outer loop, where timing is measured, the scores), with the version on every number.

**Gate:** the held-out scores reported; `pytest -m xsi` and the fast suites pass; every number traces
to a committed record. Part B's PR ready for the user. **Pause.**

## Out of scope

- A new packaged platform for 2025.1 (D3), and refitting the closed-form DSE model in `cost.py`.
- `cg_vector` beyond the move (D4); moving `mimo_cg` onto the new unit.
- Vivado synthesis or implementation numbers (licence).

## Progress log

| Date | Stage | What | Evidence |
|---|---|---|---|
| 2026-10-19 | -- | Plan drafted from a read of the page, the bodies, the test bench and the harness docs | this file |
| 2026-10-19 | -- | Revised with the user: stream-fed timing only (memory-fed optional), Part A trimmed, branch point D5, Stage 6 (scratchpad unit + outer-loop wrapper) added | this file |
| 2026-10-19 | 0 | Readiness, 2025.1 baseline, Facts A-C. Stream-fed interval 821; cause = 2-deep `reply` FIFO vs a 3-word header; depth 8 -> 390 | Stage 0 record |
