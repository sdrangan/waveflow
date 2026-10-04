# Plan: source layout — hand-written C++ in `src/`, `include/` is build output only

> **Status (2026-10-04): PLAN, nothing built.** Draft for review. Prompted by `examples/markov`,
> whose `include/` holds 29 tracked files of which 2 are authored for the example.

## Motivation

An example's `include/` currently mixes three kinds of file, and nothing on disk says which is which:

| Kind | `markov/include/` instances | Owner |
|---|---|---|
| **Hand-written** kernel bodies | `markov_gen_task.h`, `markov_chain_core_task.h` | the designer (or the agent) — real source |
| **Schema-generated** | `mkv_cmd.h`, `mkv_resp_tb.h`, `uint8_array_utils.h`, `bundle_tb.h` … | codegen from the Python `DataSchema` |
| **Framework copies** | `il_*_task.h`, `mem_*_task.h`, `streamutils*`, `memmgr*.hpp`, `mm_stream_writer_task.h` | copied out of `waveflow/build/` by the build |

Costs of the mix:

- **Edits get lost or drift.** A framework copy committed in `include/` looks like source. If someone
  edits it, the next build overwrites the change. If nobody rebuilds, it drifts away from the package copy.
- **Agents can't tell what's editable.** Generated headers carry no "do not edit" banner (the
  `<name>_task.h` from `HlsTaskCodegenStep` is the exception). An agent asked to fix a kernel has
  to guess which of 29 files is the kernel.
- **The kb index sees noise.** `waveflow/mcp/knowledge/corpus.py` uses `.gitignore` to decide
  what is source, so tracked copies get indexed as if they were examples of hand-written code
  (`examples.py:127` already has to rank `/include/` copies below their originals).

## The rule

> **`*.py`, `src/` and `xsi/<hand-written mains>` are source. `include/` and `gen/` are build output.**

- Hand-written HLS C++ for an example (task bodies, hook impls, any example-local header) lives in
  `examples/<name>/src/`.
- `include/` holds only what the build writes: schema headers, array utils, framework copies.
  It is gitignored, except where a test checks a generated file for drift (see the open decisions).
- Framework headers are never committed into an example. Their one home is `waveflow/build/`.

### Why `src/` and not `gen/include/`

My first suggestion was to move generated headers under `gen/include/` so one directory holds all
build output. I dropped it after the census: generated headers going to `include/` is wired into
every `IntField.specialize(include_dir="include")` call, `composite_gen.INCLUDE_DIR` and the TCL
`-I` flag. Moving the *hand-written* files touches 2–5 files per example. Moving the *generated*
ones would touch every codegen call site, and it buys only cosmetics once `include/` is
output-only. `src/` is also already the convention in three examples. A later rename stays
possible (see **Deferred**).

## Census (2026-10-04, tracked files)

| Example | `include/` tracked | `src/` | hand-written at root | Notes |
|---|---|---|---|---|
| markov | 29 | – | – | all of `include/` committed; 2 hand-written |
| interleaver | 31 | – | – | |
| mem_copy | 25 | – | – | `.gitignore` names 4 copied files individually; rest of `include/` mixed |
| mm_fir | 21 | – | – | tracked so the staleness guard has `include/` (`.gitignore` comment) |
| bram_access | 21 | 3 | – | tracked on purpose: `tests/build/test_wrapper_gen.py` checks for drift |
| rf_relayout | 11 | – | – | |
| rf_loopback | 9 | 1 | – | |
| regmap | 3 | – | 1 | |
| rf_blk_delay | ignored | **1** | – | **reference layout**: `src/` → copied into `include/` by `rf_blk_delay_build.py:112` |
| fir_block | ignored | – | 3 | hand-written at root, copied into `include/` (`HAND_WRITTEN_TASKS`) |
| shared_mem, block_scale, state_toy, stream_inband, vecmult, vmac | ignored | – | 1–3 | hooks at root |
| rf_samp_buf_rx/tx, rf_repeat_play, rf_shot_rx/tx | ignored | – | – | bodies are framework; nothing local |

Nine `*_build.py` files carry their own copy step (`HAND_WRITTEN_TASKS` / `FIXED_TASK_BODIES` +
`shutil.copyfile`): bram_access, fir_block, rf_blk_delay, rf_loopback (`rf_dut_build.py`),
rf_relayout, rf_samp_buf_rx, rf_samp_buf_tx, stream_inband (`poly_build.py`), vecmult.

**Re-run the census before starting** (`git ls-files examples/*/include examples/*/src`); the
table will be stale by then.

## Stages

### S0 — Framework: make `src/` first-class

Goal: an example declares nothing beyond putting files in `src/`. No per-example copy step.

1. **Compile path.** Add `-Isrc` to the csynth TCL beside `-Iinclude` (`composite_gen.py:2744`,
   `set cf "-I{INCLUDE_DIR}"`, plus any other `render_tcl` / hand-built TCL that sets `-I`). Add a
   `SRC_DIR = "src"` constant next to `INCLUDE_DIR`. `.cpp` files in `src/` go to `add_files`
   automatically, the way hook impls go through `extra_sources` today.
2. **Staleness guard — the one silent-failure risk.** `rtl_digest.source_files()` hashes
   `gen/<top>.cpp` + `include/*`. It must also hash `src/*`. Otherwise editing a kernel body does
   not mark the RTL stale and `-m xsi` gates run against old RTL (see the
   `reference-src-shrink-stales-consumer-rtl` memory). Thread a `src_dir="src"` parameter through
   `source_files`, `source_digests`, `write_stamp` and `trace_steps.rtl_staleness` (lines ~502–568).
   - Test: edit a file in `src/`, assert the guard reports stale. This test must exist before
     any example moves.
3. **Sticky hook stubs.** `HlsTaskCodegenStep.impl_dir` defaults to `output_dir` (= `include/`).
   The docstring already warns against that. Default it to `src/`.
4. **Delete the per-example copy steps** only as each example migrates (S2), not here.

Gate: the full non-toolchain suite plus `-m vitis` and `-m xsi` pass with no example moved yet.
Adding `-Isrc` to a directory that doesn't exist must be harmless; verify that on Vitis HLS 2025.1.

### S1 — Pilot: markov

1. `git mv` `markov_gen_task.h` and `markov_chain_core_task.h` into `src/`. Re-check each remaining
   file: a header with no banner and no generator might still be hand-written. Trace every file
   to the step that writes it, and treat any file no step writes as source.
2. Untrack the rest of `include/` (`git rm --cached`), and add `examples/markov/include/` to `.gitignore`
   with the same one-paragraph comment style the other examples use.
3. Update the docstrings in `markov.py` (`:179`, `:228` cite `include/markov_gen_task.h`).
4. Fresh-clone check: in a clean worktree, run the markov build and tests from nothing.
   This proves nothing depended on the committed copies.

Gate: markov's csynth (all four tops at II=1), its XSI cycle count unchanged, and the staleness
test from S0 still passing.

### S2 — Migrate the rest, one example per commit

Order: the ones with copy steps first (each deletes code), then the fully tracked ones.

- **Copy-step examples** (fir_block, rf_blk_delay, rf_loopback, rf_relayout, vecmult,
  stream_inband, bram_access, rf_samp_buf_rx/tx): move root-level hand-written files to `src/`
  (rf_blk_delay is already there), then delete the `HAND_WRITTEN_TASKS` / `FIXED_TASK_BODIES` copy step.
- **Root-hook examples** (shared_mem, block_scale, state_toy, vmac, regmap): move the hook impls into `src/`.
- **Fully tracked examples** (interleaver, mem_copy, mm_fir): sort each file the same way as in S1.
  mem_copy's per-file `.gitignore` entries collapse to one `include/` line.

Per commit: that example's csynth + XSI cycle count unchanged, cited in the commit message.

### S3 — Docs, kb, agent guidance

**Where the convention is documented.** It gets one new page, `docs/guide/flows/layout.md`
(*Example directory layout*), a child of *Hardware modules and Flows*, placed after `modules.md`.

*Does the convention differ between the flows?* The **rule** is the same in both:
`*.py` and `src/` are source, while `include/` and `gen/` are output. What differs is **which
hand-written files a flow has**, so the page gives one rule and then one table per flow:

| | Host-activated (sequential) | Free-running (concurrent) |
|---|---|---|
| hand-written HLS, in `src/` | hook impls `{kernel}_{method}_impl.{cpp,tpp}` | hook impls, plus `kernel_task()` bodies `*_task.h` |
| hand-written, elsewhere | — | `rtl_module()` Verilog; XSI testbench mains in `xsi/` |
| generated, in `gen/` | kernel `.cpp`/`.hpp`, Vitis TB `<kernel>_tb.cpp` | composite top `<top>.cpp` |
| generated, in `include/` | schema headers, array utils | schema headers, array utils, framework task copies |
| generated, in `xsi/` | — (Vitis cosim) | harness, ports header, framework BFM copies |
| verified by | Vitis csim / cosim | XSI gates + staleness guard (which hashes `src/`) |

(Check this table against the code when writing the page; it reflects the census, not a re-read
of every step.)

Why `flows/` rather than `patterns/`: `patterns/` describes design *shapes* (command–response,
continuous stream) and does not depend on the flow or the build. A directory layout is a property
of the *realization*, and `flows/` already describes itself as "which build steps run, producing
which artifacts". The layout belongs with the artifacts. `build/` was the other candidate; it is
about the step/DAG machinery, so a reader asking "where do I put my file" would not look there.

Links into it, so a reader finds it from the page where they are writing the file:
- `flows/sequential_flowsteps.md` and `flows/concurrent_flowsteps.md`: one line each, pointing
  to that flow's table.
- `custom_hooks/writing.md:65` (which already says how codegen *finds* the impl file): add
  "it lives in `src/`" plus the link. Do the same in `custom_hooks/body_only.md` and
  `comp_codegen/freerunning_override.md` (`kernel_task()`).
- `build/codegen.md`: "codegen writes into `include/` and `gen/`; never edit them", plus the link.
- `patterns/command_response.md`: no change. It links to the examples, which will show the layout.

**Other edits:**
- `docs/guide`: 23 pages mention `include/`. Fix the ones that say where hand-written code goes
  (`custom_hooks/body_only.md:123`, `rf/rfshotbuf/tx_internal.md:32`). Leave the ones that
  correctly say codegen writes there (`build/codegen.md:161`).
- `waveflow/mcp/knowledge/examples.py:101-127`: the "source copy wins over the `/include/` copy"
  ranking becomes unnecessary once copies are ignored. Simplify it, and check `kb examples` output.
- `waveflow/mcp/frames/stream_inband/{frame,process}.md`: the agent frames tell the agent where
  to write. They must say `src/`.
- `CLAUDE.md`, *Writing HLS kernel bodies*: add one line, "hand-written bodies go in `src/`;
  never edit `include/` or `gen/`".
- The AI-accel lab / hook-first flow prompts (`plans/hook_first_flow.md`, `accel_mcp*`): same.

## Open decisions (for the user)

1. **Committed generated output.** bram_access and mm_fir track `include/` / `gen/` on purpose
   (a drift test; the staleness guard). With `src/` hashed by the guard, mm_fir's reason goes away.
   bram_access's drift test is a real reason. Options: (a) keep tracking *generated* files
   there, never framework copies; (b) have the drift test regenerate into a temp dir and diff.
   I'd pick (b).
2. **`xsi/` has the same mix.** Framework copies (`xsi_bfm.h`, `xsi_loader.*`, `xsi_rfdc*.h` …) are
   committed beside hand-written `*_tb.cpp` mains in at least fir_block and rf_shot_tx. The same
   rule fixes it (hand-written mains → `xsi/src/` or stay, copies ignored), but it is a separate
   generator (`xsi_workspace.py`). It could go in this arc or the next.
3. **Banners.** Independently of layout, should every generated header carry a
   `// GENERATED by <step> from <source> — do not edit` line? It's cheap, and it helps anyone
   reading a single file. I'd do it in S0.

## Deferred

- Moving generated headers from `include/` to `gen/include/` (one output root). Revisit only if
  `include/` vs `gen/` still confuses people after this lands.
- `examples/_archive/`: leave as-is.
