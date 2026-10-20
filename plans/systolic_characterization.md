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
8. **Correction (after the fix below):** the "one word per 2 cycles" store output above was the
   scratch `framed_to_axis` adapter, not the store: with the real framed port the store writes `C` at a
   word a cycle (128 words in 128 cycles) and the reply header leaves without waiting (16, 17, 18).

#### Record: the fix (2026-10-19, user: do every recommendation; 2024.1 numbers may go)

D1-D6 decided as recommended. Changes:

- `SystolicUnit` and `CgVectorUnit`: the `reply` channel is `2 * header_words` deep (6 at 64 bits).
- Framework: `waveflow.hw.hw_module.build_params(module)` -- the `HwParam`s plus a class's
  `build_fields` -- used by `harness()` and `system_top._rtl_of`; `SystolicUnit.build_fields = ("a",
  "b", "c")`.
- `SystolicRx.s_in` / `SystolicStore.s_out` are `FramedStreamIF*`, their `KernelTask`s mark them
  `framed`, and the bodies take the boundary word type as a template parameter (`IN_T`, `OUT_T`);
  `wf_linalg::drain`, `wf_store_matrix` are generic in the stream's word type, `read_header` added;
  the traits' `*_mem` structs forward `write_boundary_lane`.
- The RTL tests take the tool label from the csynth banner.

**Stream-fed, the real unit in its own harness (no scratch fixes):** all steps ok, compare clean,
bit-exact; **steady interval 390** (`[707, 1097, 1487, 1877]`). Per-job schedule from the trace:
architecture page, *The schedule, measured*.

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

#### Record: Stages 1-5 (2026-10-19), done in one pass at the user's request

The user approved every recommendation (D1-D6) and allowed the 2024.1 numbers to go; the fix went in
first. All numbers Vitis HLS / Vivado xsim 2025.1.

- **Stage 1** (commit `3ff39dd8`): pages moved (`dsp/linalg_common.md`, `dsp/systolic/index.md`,
  `dsp/cg_vector.md`), links rewritten by resolving each relative link from its old and new place,
  DSP index lists them, `CLAUDE.md` and the docs-number test paths updated. `tests/docs` pass.
- **The fix** (record above): `reply` depth `2 * header_words` in both linalg units.
  - Memory-fed gates `pytest -m xsi tests/linalg --basetemp=C:/wfsc/x1`: **5 passed** (CG centre,
    systolic centre / one_word_groups / smallest / word32), bit-exact. Centre run ends at 1,690
    (was 2,237); job 1 at 854 (was 1,088).
  - **Memory-fed steady interval: 390, 390, 390** (4 identical `8x8x32`, `UnitBench`) -- equal to
    stream-fed. The 2024.1 "sum of compute and transfer" (720) was this serialization; the `m_axi`
    pointer-FIFO explanation was wrong (Stage 0 item 7 settled).
- **Stage 2:** `docs/guide/dsp/systolic/architecture.md` -- the four tasks and channel depths, the
  storage table (18 BRAM18K, all in the three ping-pong buffers; the core's banks in LUTRAM and
  registers), the `X` pre-skew and the `B` column banks, load, the level-2 tiling and the skewed sweep,
  store, the level-1 outer loop as not built, and the measured schedule (cmd 201, `X` freed 223, `B`
  freed 291, tile outputs every 36 cycles from 321, `C` committed 589, next cmd 591).
  Deviation: the prediction was not written before the measurement -- the trace came first (Stage 0).
- **Stage 3** (D2(a)):
  - framework: `TaskInst.params`; the trace manifest gives a block channel its lock nets
    (`blk_release`, `blk_ce0`, `blk_full_n` / `blk_empty_n`, optional roles); `load_trace(scope=)`
    binds at a nested kernel's scope; `BoundTrace.fifo_beats` / `block_events` (a writer's commit
    marked held when `i_full_n` was low the cycle before); `SobIFMaster` / `SobIFSlave` log blocks as
    transfers (`first` = acquire, `last` = release); `calib.phases.internal_xfers`;
    `calibrate_system` reads a timed task inside a kernel at the kernel's scope and skips a phase the
    workload never exercised; the harness offers a calibration workload when any task in the tree is
    timed.
  - the core: phases `start` (`cmd_in.last` -> `a_blk.first`), `copy_x` (`a_blk.first` ->
    `a_blk.last`, `ag`), `to_b` (`a_blk.last` -> `b_blk.first`), `copy_b` (`b_blk.first` ->
    `b_blk.last`, `bg`, every B of a job), `sweep` (`b_blk.last` -> `c_blk.last`, `tiles`, `steps`);
    pysim waits them (`timeout`), replacing `cost.core_interval`. A copy is measured from its block's
    acquire (`timeout` from an open block, `SobIF*.open_block`): the first declaration measured from
    the previous event, and the MIMO-CG detector -- whose core waits for the vector core's `P` -- showed
    pysim hiding each 36-cycle copy inside the wait (`test_python_simulation_takes_the_cores_cycle_models`,
    now START + nit (copy B + sweep + STEP), exact on its three configurations). The unit:
    `jobs` / `workload` / `golden` / `calib_holdout = {"tiles": (3, 6)}`.
  - harness DAG `tests/linalg/_systolic_harness.py`; tests `tests/linalg/test_systolic_harness.py`
    (manifest, reply depth, scenarios; `-m xsi`: 4 jobs stream-fed, intervals `[390, 390, 390]`);
    `WANT_XSI_GATES` 234 -> 235.
- **Stage 4** (centre only; the second configuration of the plan not run -- see below):
  `python -m tests.linalg._systolic_harness --root C:/wfsc/sh --through calib_phases`, 32 jobs, filed in
  `waveflow/linalg/calib/` under `systolic_core-65ffc80e`, provenance `vitis 2025.1`:
  `start = 5`, `copy_x = 1 + ag`, `to_b = 3`, `copy_b = 1 + bg` (exact, 32 rows each), `sweep = 8.3 + 18.2 tiles + 1.0 steps`
  (23 fitted, mean 0.76, worst 1.30; 8 held out, mean 0.68, worst 0.92; 1 held -- a short job after a
  long one waited for the store's release).
  - **Finding:** the RTL ping-pong lets a writer commit only after the reader frees the previous
    block (`full_n` drops at count 1); pysim's `StreamOfBlocksIF` lets it commit two. pysim is
    optimistic when the reader is slower. Not fixed: it changes every block user's pysim timing.
  - **Finding:** nothing in the framework sets `BuildConfig.vitis_version`, so phase records filed by
    other builds say `"tool": "vitis"` with no release; this build sets it (`build_config`).
  - Not done: the second configuration, the hostile-sink run (`add_robustness_steps`).
- **Stage 5:** `docs/guide/dsp/systolic/timing.md` (where measured, phases, calibration, the fix,
  limits); the main page's 2024.1 cycle claims replaced (example prints the phases' prediction
  `[22, 68, 298] 388`; *Cycles* rewritten; the accuracy table's two cycle rows, the isolated-rejection
  and cycle-model limits removed; resources stay, labelled 2024.1); `cost.py`'s docstring;
  `timed_phases.md` and `units_calibration.md` on block events and inner tasks; docs-number tests
  updated and one added (the timing page's fit against the filed record).

# Part B -- the scratchpad unit and the outer loop

> **Revised 2026-10-19 (user).** (1) Fold the command changes for the outer loop (matrices that do
> not fit on chip) into the same redesign. (2) Do **every** optimization before an extensive
> calibration: RTL runs are slow, so iterate on pysim bit-exactness + csynth (II, resources) + one
> short stream-fed RTL run per change (about 6 minutes), and calibrate and run the full gates once at
> the end (6d). (3) Optimize buffers and pipelines freely -- the code base is early -- and flag pages
> and examples whose numbers change with "needs re-measurement" rather than holding back.
>
> Optimizations found in Part A, to design in (centre `8x8x32`, 390 cycles a job, all core):
> - **Tile overlap.** A tile costs 36 cycles for an 18-step sweep: the fill/drain and its 8 output
>   groups do not overlap the next tile's sweep. Shadow accumulators drained into `C` during the next
>   sweep: about 18 + a few a tile (298 -> ~160 for 8 tiles).
> - **No core-side copy.** The core copies `X` and `B` out of the block buffers into its banks
>   (22 + 68 cycles) before the first tile. Operands loaded straight into the banked layout the array
>   reads, double-buffered, remove it.
> - Together about 390 -> 170-200; then the input stream (163 words at a word a cycle) is the bound.
> - **pysim's block buffer** (framework): the RTL ping-pong releases a writer's commit only after the
>   reader frees the previous block; pysim allows two. Fix pysim, flag what it changes.
>
> **Decided (user, 2026-10-19): ordered, banked stream-of-blocks** -- not addressed regions.
>
> - **Fact (measured, Vitis 2025.1, `C:\wfsc\sobbank`):** `ARRAY_PARTITION dim=1 complete` on a
>   `hls_thread_local hls::stream_of_blocks<v_t[R][K], 2>` splits it into `R` independent ping-pong
>   buffers (`b_0`..`b_3`, "aggregating stream-of-blocks variable 'b_i'"); a reader takes all `R`
>   rows a cycle at II 1, the writer fills at II 1. So operands are loaded straight into the layout
>   the array reads -- no core-side copy -- and the ping-pong double-buffers.
> - **Why not addressed regions:** tasks share no addressable memory, only FIFO-ordered blocks.
>   Addressed regions need hand-written T2P BRAMs (`T2pBram`/`BramIF`, `LockedT2pMemIF` with disjoint
>   regions, plus ready/free channels -- the lock arbitrates, it does not synchronise): `R + C`
>   memories and the wrapper. A later upgrade if a use needs arbitrary reuse; it replaces the buffers
>   and the command's reuse fields, not the sweep. Accepted price now: every block is sized to the
>   maximum (`Mmax x Kmax`, `Kmax x Nmax`, `C` at `acc_t`), x2; small jobs leave it idle (mostly LUTRAM,
>   since the banks are shallow).
> - **Command (order, not address):** `MatMult(tx_id, m, n, kp, npanels, nb, keep_a)` -- for each of
>   `nb` outputs, for each of `npanels` k-panels: take the next `A` (unless `keep_a`: one `A` for the job)
>   and the next `B`, sweep accumulating into the `C` tile at `acc_t`; then round once and emit `C`. The
>   outer loop (`npanels` > 1) is exact for `K > Kmax`; CG reuse is `keep_a` with `nb`. `Load` and
>   `Store` are not separate commands: operands arrive in order, `C` leaves per output.
>   `tx_id` and a `Status` per command on its own stream; errors fatal.
> - **The core as a streaming systolic array:** each PE carries a `first` flag with its `X` input and
>   latches its accumulator into a shadow register when it arrives, starting the next tile at once;
>   tiles follow every `P = max(k_p, R*C/L, L)` cycles (a tile shorter than `P` is padded with zero
>   steps -- exact); shadows drain in diagonal order (group `(i, cg)` from its row's latch to the next
>   overwrite). Centre: `P = 8` against 36 a tile now; core ~ 80-90 cycles a job against 390; the
>   unit then bound by its input (163 words a job): ~165.
> - **Build order** (each: pysim bit-exact -> csynth II/resources -> one short stream-fed RTL run):
>   (1) banked loader + core reading in place; (2) the streaming sweep + shadow drain; (3) panels;
>   (4) receiver, store, command, pysim module; (5) the wrapper. Calibration and full gates once, last.
> - The current `SystolicUnit` stays beside the new unit (`mimo_cg` uses it).

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

#### Record: Part B built (2026-10-19, branch `systolic-engine`) -- before the calibration

All numbers Vitis HLS / Vivado xsim 2025.1, `xczu48dr`, 4 ns, centre (`Mmax 8, Kmax 8, Nmax 32, Pmax 4,
R 4, C 8, L 4`). Guide: `docs/guide/dsp/systolic/systolic_engine.md`. Gates: `tests/linalg/test_systolic_engine.py`.

- **Facts measured first** (`plans/witness/systolic_stream`, `C:\wfsc\sobbank`, `C:\wfsc\sobnest`):
  a partitioned `stream_of_blocks` is one ping-pong per bank, read a bank a cycle at II 1 (2-D
  partitions too); one task may hold two read locks and a write lock at once (csynth; the pipelines
  fall back to `style=stp`); the streaming sweep prototype is bit-exact, II 1, 84 steps for `8x8x32`.
- **Built:** `waveflow/linalg/systolic_engine.py` (`SystolicEngine`: rx, two loaders, core, store) and
  `systolic_engine_mem.py` (`SystolicEngineMem`: sequencer, in-band `MemRStream` / `MemWStream`,
  writer); bodies `waveflow/build/systolic_engine_{rx,load,core,store,mem}_task.h`; framework
  `StreamOfBlocksIF.bank(shape, partition)`; pysim's block buffer now commits one block ahead.
- **Design changes found while building** (each from a measurement):
  - two loaders, not one: a single loader filling `A` then `B` waits to commit the next `A` until the
    core frees the current one, which it does together with `B` -- the next `B` would load after the
    sweep;
  - the `A` channel holds a whole `A` (32 words): the `A` loader takes a value a cycle and the receiver
    forwards in order (206 -> 175 cycles a job);
  - one call site of the array: three call sites (keep, last panel, other panels) built three arrays
    (410 DSP); the `C` lock is held across an output's panels and `keep_a` moved into the `A` loader
    (repeats a local copy) -- 137 DSP for the core;
  - `B` laid out `[Kmax][Nmax/C][C/LW][LW]` so a word's values land in constant lanes (a provably
    conflict-free write a word a cycle); `A` written a value a cycle (no `kp` alignment rule; `A^H`
    at a value a cycle, against II 2 in `SystolicUnit`);
  - LUTs 47.4k -> 37.0k: the accumulator store in LUTRAM (`ram_s2p`), one adder per element, the tile
    index riding with the flag, 10-bit counters.
- **Measured:** engine bit-exact at RTL (six commands); **175 cycles a job** stream-fed `8x8x32`
  (`SystolicUnit` 390; 162 words in + 13 of command handling); wrapper exact at RTL on four jobs incl.
  `32x32x64` (done at 6,655 against an input floor of ~5,136); engine 154 DSP / 37.0k LUT / 26 BRAM,
  est. 3.05 ns; wrapper 156 DSP / 43.2k LUT / 30 BRAM, est. 3.35 ns.
- **The core's own time** (trace: its first touch of `X` to its `C` commit): **87 cycles** a job
  (panel 83 + commit 4), the same at `kp` 1, 2 and 8. Never the bound at 64-bit words: the input binds
  at `kp = 8` (175), the output at short `kp` (141 cycles: `C` is 128 words whatever `kp`).
- **The wrapper's reader** (the `32x32x64` product, read port traced): the in-band `MemRStream` paid
  ~16 idle cycles per read command, 8 a tile (6,655, 80% busy). Replaced by `EngineFeeder`, a task
  owning the read port that streams a tile per firing, a burst loop per operand panel: **6,470** (82%;
  ~12 idle cycles at each switch between burst loops). Tried and dropped: one flattened loop with a
  computed address (no burst, 10,639); `A` panels then `B` panels as two runs (`B` waits behind the
  core's last panel, one block of buffering: 7,984). Wrapper now 157 DSP / 42.9k LUT / 30 BRAM.
- **128-bit words** (branch `systolic-wide`): `lanes.WORD_BITS_SUPPORTED` takes 128; the engine's
  status port stays 64 bits (`HwModule.endpoint_port_widths`); the harness memory (pysim `MemoryMod`,
  RTL `FlatMemory` and the AXI-MM slave BFMs) takes words of k uint64 chunks; `write_array` no longer
  reads a wide word's chunk axis as an array axis (it broke `MemWStream` at 128). Engine **103 cycles
  a job**, bit-exact, now **core-bound** (trace: `C` held 83, ~20 idle between jobs; 81 input words
  take ~94); 154 DSP / 36.9k LUT / 30 BRAM. Wrapper exact, `32x32x64` done at **3,693** (input floor
  ~2,570; the binding stage not traced); 157 DSP / 43.1k LUT / 38 BRAM.
- **Found:** the trace manifest predicts `<task>_<template args>_U0`, but Vitis 2025.1 names the
  engine's loaders and store `<task>_U0` (at 64 bits too), so `--trace` binds only the core's nets
  and the banked `a` / `b` lock nets do not bind. Fix before the calibration (6d).
- **Continuous panels** (branch `systolic-panels`): the core keeps the array, skew delay lines on its
  edges (every row and column read at one step, so a panel's reads fall in its own `T·P` steps) and a
  two-slot drain queue across a command's panels and outputs; tile `g` drains `L + n` steps after tile
  `g+1` starts; a gap before a panel whose tiles would read unstored partial sums (`T` = 2, 3); one tile
  keeps accumulating (`S_KEEP`); one empty tile runs the last out at each command's end. `C` leaves in
  drain order on a FIFO to a new `EngineCollect`, which fills the `C` block. **Found:** carrying the
  state across firings as `static` registers made csynth run out of memory (6.7 GB against 0.4; core
  alone with locals: 0.36 GB, II 1) -- so the state is per command. **Measured (2025.1):** bit-exact
  (six commands); `8×8×32` at **94** cycles a job with 128-bit words (was 103) and 175 with 64-bit;
  the 128-bit `short` workload is also 94, so at 128 bits the core's floor (64 tile steps + ~30 a
  command: the 12-step run-out and ~18 of restart) TIES the input (~94) -- the receiver fix alone gains
  nothing; carrying the array's state across commands is no longer optional; core LUT 36.9k -> 31.2k, FF 25.7k -> 36.3k, BRAM 30 -> 36, est. 2.92 ns. The wrapper
  barely moved (3,693 -> 3,680; 6,470 -> 6,468): its **read port** binds (~460 cycles a tile against
  ~270 of the engine).
- **Docs restructured** (2026-10-19, user): `SystolicUnit` retired from the guide -- pages: index (which
  module), `systolic_theory` (one panel, the bounds, memory), `systolic_engine`, `systolic_engine_mem`;
  the unit's timing and resource tests deleted, its functional tests and all of `examples/mimo_cg`'s
  quarantined (`_Q_CG`) until mimo_cg moves to the engine (the student's). `docs/examples/systolic`
  (timing and resource model, from the harnesses) comes with the calibration.
- **Open before the calibration:** the feeder's loop switches (`hls::burst_maxi` or a deeper `B`
  buffer) -- now the wrapper's bound; the receiver's ~13 cycles a command AND the core's ~30 a command
  (tied at 128 bits; the core's needs the state carried across commands, without `static`); the
  trace-manifest names above; a small-engine csynth against `SystolicUnit`'s area; then calibrate once
  (6d).
- **After the calibration** (user, 2026-10-19): `docs/evaluation/sim_speed/systolic.md`, the twin of
  `fft.md` -- one bit-exact workload run as the untimed model (`waveflow.linalg.matmul`), pysim (the
  engine's harness, and the wrapper's) and the RTL under XSI (plus csynth per point), the cost per
  product, from an `examples/systolic/systolic_runtime.py`. Set up the script and the page and take
  **preliminary** timings on the development laptop, labelled so; the final numbers come from a run
  on the lab machine (the student's). Event counts (pysim events, RTL cycles) can be taken anywhere.

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

### Stage 7 (late, low priority) -- compare with AMD's Vitis BLAS GEMM

The student's core is said to be based on AMD's Vitis core (nothing in the repo records which parts);
AMD benchmarked theirs extensively ([Vitis BLAS benchmark](https://docs.amd.com/r/en-US/Vitis_Libraries/blas/benchmark.html);
their systolic array is the L1 `gemm` primitive under an L2 GEMM kernel, sized to the memory width,
operands streamed from DDR/HBM). After Part B, not before it:

1. **Read their source against ours** (cheap): does their array overlap a tile's drain with the next
   sweep, stream operands into the array rather than copy them into banks, double-buffer across jobs?
   A record of what the student's derivation lost, and whether Part B recovered it.
2. **Wrap their GEMM and run it in the same harness** (bigger; `plans/integrate_blas.md`, where GEMM
   may be L2-only): the same jobs, cycles per job and resources side by side. Caveat: their published
   numbers are large float/int GEMM; at 8x8x32 complex fixed point their fill and tile setup may
   dominate, so architecture, not their throughput, is the comparison.

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
