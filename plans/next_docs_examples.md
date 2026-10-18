# Next steps: examples, the harness section, and the docs review

A hand-off plan, written 2026-10-18 at the end of the session that restructured the guide.  It is meant to
be picked up by an agent with no memory of that session: everything needed is here or linked.

## Where things stand

- **The guide's narrative is the Flow Overview**, `docs/guide/systems/` (title *Flow Overview*): one page
  per step of the flow, each walking the free-running polynomial (`examples/stream_kernel`), with steps
  up to `block_eq` and `markov`:
  `index.md` -> `dut.md` (*The DUT and two levels of simulation*) -> `streams.md` -> `command_response.md`
  -> `hwmodule.md` (*Defining the kernel in Python and HLS*) -> `pysim.md` -> `rtl.md` ->
  `verification.md` -> `calibration.md` -> `dse.md`.
- `docs/guide/flows/` is now the **Modules** reference (what a module is, writing one, module by module,
  XSI components, parameterization, toward the board).  `patterns/`, the old recipe and the old flow
  pages are `nav_exclude` stubs pointing to the new pages.
- The plan behind all of it is `plans/guide_system_first.md` (decisions D1-D10, Stages 0-7, records).
  Stage 7 (the restructure) is done in a first pass; Stage 5 (planning-only blind tests on the new tree)
  is done once per rung; Stage 6 (two full builds, blind) is done.
- **The examples survey** is `plans/examples_order.md`: the proposed order with each example's
  increment, the legacy (host-launched) ones, what needs work, what to delete.  Read it first for tasks
  B-D.
- **Blind-test materials** are in `evaluation/planning_tests/` (specs, answer keys, grader, results);
  run with `python -m waveflow.cli blind-test` (see `results/baseline_2026-10-17.md` for the exact
  command and message).
- Branch: the work was on `guide-stage7`; check whether it has been merged to `main` before starting
  (`git log origin/main --oneline | head`).  Start each task on a new branch from `main`.

## Working rules (this repo)

- Read `CLAUDE.md`.  Run everything with the project venv: `../pysilicon-venv/Scripts/python.exe -m pytest ...`
  on this machine (a bare `pytest` may pick another interpreter and run nothing).  Machine details are in
  `CLAUDE.local.md`.
- The dev loop: `pytest -m "not vitis and not xsi and not gem5"` (the knowledge-index timing test,
  `test_index_builds_in_under_three_seconds`, fails on a busy laptop -- known, ignore it).  Docs:
  `pytest tests/docs` (links, anchors, snippets, nav-title collisions); MCP: `pytest tests/mcp`
  (frame lint, retrieval eval).
- RTL gates: `pytest -m xsi`.  A change to a header every example copies (`waveflow/build/streamutils_hls.h`
  and the like) stales every example's RTL; then run `WAVEFLOW_RESYNTH=1 pytest -m xsi` once (~40 min).
  The mimo_cg unit bench's csynth sometimes drops its `m_axi` bundles (a Vitis 2025.1 nondeterminism);
  `run_vitis_hls` retries once, and a rerun passes.
- Commit style: one PR, many commits; each commit message ends with the co-author line.  Push and merge
  are separate, standalone commands (no `cd` prefix, no `&&` chains).
- Docs style: a page says what is, with a link per claim; name flow steps and link them, never "Step N";
  learning objectives are testable skills, in flow order, never "why".  Every measured number names its
  tool version (Vitis/Vivado 2025.1 here).
- **Laws that cost the most when forgotten**: never pack or unpack a word by hand (use the generated
  `<elem>_array_utils` lane routines and schema readers); a deadlock looks like success, so gates assert
  counts; a credit producer's writes are guarded on `wait_room()`'s result; `.gitignore` drops `*.json`
  (check-ignore any new JSON); `examples/` is an installed package (re-install after packaging edits);
  `structure_signature` keys on a class's `__module__` -- moving a class file moves its calibration
  records.

## The tasks, in order

### A. Split the polynomial example into two pages sets

Today `docs/examples/stream_kernel/` is one example: on streams (`index`, `protocol`, `golden`,
`kernel`, `system_flow`, `timing`) and, as its last page, behind AXI-MM (`memory_mapped.md`).  Make the
bus version its own curated example, **pointing at the same code** (`examples/stream_kernel/`,
`stream_kernel_mm.py`):

- a new `docs/examples/stream_kernel_mm/` (title e.g. *The polynomial on a bus*), grown from
  `memory_mapped.md`: the device built from `mm_views`, the crossbar, the two-thread `SwHost` and its C++
  twin, interrupts and no polling, the device's own harness (`mm_system(direct=True)`), the composition
  check (+1.8% of RTL), its learning objectives (only what it adds over the stream version);
- `example_dir: examples/stream_kernel` on both (the curated set is keyed on `example_dir:`; check how
  `waveflow kb examples` and `tests/mcp/test_knowledge_corpus.py` count and list examples -- two pages
  with one dir may need the corpus to allow it, or a `example_name:` key);
- `memory_mapped.md` becomes a stub; links to it rewritten (docs, frames, the Flow Overview's ladder in
  `systems/rtl.md#a-ladder-of-systems`);
- the `bus_system` frame's `reference_examples` (`waveflow/mcp/frames/bus_system/frame.toml`) lists the
  new page first;
- the curated count in `CLAUDE.md` ("twenty-two") and in the corpus test.

Gate: docs and MCP tests; `waveflow kb search "put a kernel on a bus"` finds the new page in the top 5
(add it to `tests/mcp/test_retrieval_eval.py`).

### B. Fill in block_eq

`docs/examples/block_eq/` (index, protocol, stages, system_flow) is thin next to stream_kernel.  Add:

- **A theory page for a reader who is not a wireless engineer** (the user), first in the page set: what a
  channel does to a transmitted symbol (`r = h s + noise`, `h` a complex gain per subcarrier or row), what
  an equalizer is for (undo `h` to recover `s`), why the gain is `g = conj(h) / (|h|^2 + N)` rather
  than `1/h` (dividing by a small `h` amplifies the noise; the noise term `N` limits the gain -- the
  MMSE equalizer), why one gain serves a whole row (the channel is flat across it), and what the example
  computes in those terms.  Plain words and one small worked number; no derivation beyond what the
  arithmetic needs; link out for the full theory.

Then: the golden and its float32 operation order; why the split is where it is (two computations at different
rates); the broadcast's lockstep and the rule it imposes; link depth sizing; each stage's HLS body walked
like `stream_kernel/kernel.md`; the four systems on one DAG; learning objectives (the increment over
stream_kernel).  Read `examples/block_eq/*.py` and `src/*.h` for the facts; do not invent numbers --
anything measured comes from a run.

### C. Reorder the examples (from `plans/examples_order.md`)

- Set `nav_order` on each `docs/examples/*/index.md` for the core path: basic_vec, stream_kernel (streams),
  block_eq, stream_kernel_mm (bus, from task A), memcpy, interleaver, fir_block, bram_access, mm_fir,
  markov; then measuring/exploring (vecmult, xbar_profile, mimo_cg); then the domain examples; then a
  **Legacy** group for the host-launched three (regmap, stream_inband, shared_mem) -- a parent page under
  Examples, like `docs/guide/legacy/index.md` does for the guide.  Check `docs/examples/index.md` (the
  examples landing page) and keep it in step.
- Learning objectives on every curated page, in stream_kernel's form, stating only the increment.
- **Keep every advanced example** (the user): the RF examples (`rf_loopback`, `rf_shot_tx`, `rf_shot_rx`,
  `rf_shot_loopback`, and the RF component directories `rf_blk_delay`, `rf_relayout`, `rf_repeat_play`,
  `rf_samp_buf_rx`, `rf_samp_buf_tx`), the FFTs (`ssr_fft`, `vitis_fft`), `mimo_cg`, `cpu_sched`,
  `xbar_profile`.  They move later in the order, never out of the tree.  Deletion is only for the
  directories listed next, which nothing uses.
- Delete the dead directories: `examples/vscode`, `examples/dse_fir` (fix the corpus test that lists
  it), the untracked local `bram_toy`, `test`, `vecunit`.  `vmac` and `_archive` need their references
  moved first (two guide pages and two tests for vmac; two tests for `_archive`) -- do those or record
  why not.
- Optional, larger: move memcpy, interleaver, fir_block, bram_access and the RF examples from
  `GraphSystem` subclasses to `HwSystem(sim, dut)` -- markov and mm_fir were moved this way (see
  `markov_system()` in `examples/markov/markov.py`); gates must stay identical (pysim cycles and RTL
  cycle counts), one example per commit.

### D. The harness section

A guide section on harnesses: `docs/guide/flows/units.md` (*Developing module by module*) is most of the
material, and `waveflow/hw/harness.py` the source.  Before writing it, close the two gaps the blind builds
hit (each with a test):

1. **No stand-in for a kernel's memory-master port**: `harness()` raises `NotImplementedError: ...
   port 'm_mem' is a MMIFWriteMaster -- only stream ports have a stand-in yet` (`harness.py`, the
   port loop).  A memory model behind a direct link is the natural stand-in (`memory_harness` already does
   the reverse: a memory driven by a scripted master).
2. **The input side's acceptance cycles**: `StreamDriver.accept_bundle` (a `DynParam`) makes the RTL
   `AxisMaster` record when each word was accepted, but `harness()` never sets it, so a harness run has
   no input-side timing.  Set it, and document where the recordings land.

Then the section: what a harness is (any module as the DUT of its own `HwSystem`), the stand-in per port
type and each one's RTL twin, `workload(kind)`, reading recordings, hostile stand-ins
(`StandInPolicy`, `add_robustness_steps`), calibrating in a harness, composing a parent.  Place it under
Modules or as its own section; the Flow Overview's `pysim.md#the-cut` links to it.

### E. Review the Overview section for consistency

The site's top-level *Overview* section -- `docs/overview/` (`index`, `motivation`, `flow`, `pymodel`,
`aiharness`, `salsa`) and the home page `docs/index.md` -- was written before the restructure.  Read it against the Flow Overview: the terms (DUT, harness,
`HwSystem(sim, dut)`, the two levels of simulation, free-running vs host-launched), the claims, the
example names and the links.  Fix what disagrees; keep measured numbers only with their source.

### F. MCP

- **Frames and examples**: each frame's `reference_examples` against the new example order and the new
  bus page (task A).  `freerun_pipeline` and `stream_pipeline` overlap -- make each frame's `choose_when`
  say plainly which to choose (memory adaptors or not).
- **Stage 5's second runs**: rerun `01_qam16_llr` and `03_peak_detect` plan-only (same command and message
  as `evaluation/planning_tests/results/stage5_2026-10-18.md`), grade with `plan_grader.py <folder>
  --spec <name>`, record beside the first runs.
- Open framework items the blind tests found (each fixable): one kernel class at several widths (no
  supported path; agents write a thin subclass per width -- `cpp_kernel_name` is class-level); an
  `ArrayUtilsStep` element that is a `DataList`; no `abs` in `waveflow.hw.fixpoint`; a framework hook to
  check pysim against the golden on the DAG (today in pytest).
  **Done (2026-10-18)**, one commit each:
  - `kernel_name_params` (`HwModule` ClassVar) + `hw_module.kernel_name(mod)`: a top per parameter set,
    `<cpp_kernel_name>_<param><value>`, used wherever the system flow names a top; a class listing none is
    unchanged.  Docs: `flows/parametrization.md#kernel-name-params`.  Test:
    `tests/build/test_kernel_name_params.py` (two widths, two harnesses, one DAG -> two csynth tops).
    **Remains**: no example uses it, and no csynth / XSI run of two widths has been made (the naming is
    checked, the RTL is not); the hook flow (`param_supports`, `derive_kernel_task`'s `<kernel>_task.h`)
    stays class-level.
  - `ArrayUtilsStep` on a `DataList`: it already worked; now checked (`tests/hw/test_arrayutils_datalist_elem.py`,
    MinGW 10, pf 0/1/2/4), and a record wider than a word without `DataSchemaStep(framed=True)` is a
    `ValueError` at `dag.add` instead of a C++ error.  Docs: `build/codegen.md` (*A `DataList` element*).
    **Remains**: not run through Vitis csim / csynth.
  - `fixpoint.abs` (`<W+1, I+1>`, lossless) and `DataArray.__abs__`; Vitis csim bit-exact test.  Docs:
    `schema/python/fixpoint.md#magnitude`.
  - `GoldenStep` (`golden`, after `pysim`): `HwSystem(golden=...)`, a harness's from `module.golden(kind)`;
    wired in stream_kernel (`PolyKernel.golden`).  Docs: `build/xsi_system.md#golden`,
    `systems/verification.md`.  **Remains**: `docs/examples/stream_kernel/system_flow.md` ("Checking pysim
    against the golden") names the step now; a
    system with a `SwHost` has no golden step (its traces are the host's), nor do calibration systems.

## Status (2026-10-18, branch `next-docs-examples`)

A-F done on one branch, one merge per task:

- **A** `docs/examples/stream_kernel_mm/` (5 pages); `memory_mapped.md` a stub; two cards may share one
  `example_dir` (`test_two_pages_may_share_one_example_dir`); `bus_system` lists it first.
- **B** block_eq: `theory.md` (first), `golden.md`, `hls.md` new; `stages.md`, `system_flow.md` rewritten.
  No timing page: block_eq declares no timed phases.
- **C** order 1-20 + `docs/examples/legacy.md` (a file, not a folder: every `examples/<dir>/index.md` is a
  card); objectives on every curated index; `vscode`, `dse_fir`, `_archive` deleted.  **vmac kept**
  (reason in `examples_order.md`: `custom_hooks/complex.md` teaches a hook walking an `m_axi` pointer,
  which no current example has).  memcpy, interleaver, fir_block, bram_access, rf_loopback, rf_shot_tx,
  rf_shot_rx moved to `HwSystem(sim, dut)`, pysim and RTL byte-identical (table in `examples_order.md`).
  **Left**: the untracked `examples/bram_toy`, `examples/test`, `examples/vecunit` in the main checkout
  (the agent's sandbox refused the delete; `test/dataschema2.ipynb` and `vecunit/vecunit.py` are
  hand-written -- check before deleting); `pip install -e .` after merging (pyproject changed).
  `GraphSystem` still used by state_toy, the RF component dirs and the two FFT wrappers.
- **D** memory stand-in for `MMIF*Master` ports (pysim + RTL twin = `MemoryMod.bfm_model()`; mem_copy's
  harness gates at 2908); `accept_bundle` set by `harness()` (`xsi_run/vectors/<port>_acc/`);
  `units.md` -> a page set (`units_harness`, `_recordings`, `_hostile`, `_calibration`, `_composing`).
  **Left**: no hostile memory (the C++ AXI-MM slaves have no stall knob).
- **E** Overview pages and the home page against the Flow Overview, incl. `status.md`, `targets.md`.
- **F** frames (`choose_when` decided by memory; firblock a freerun reference); the four framework items
  above; Stage 5's second runs recorded in `stage5_2026-10-18.md` (both "would build, with small fixes").
  **Left** from the second runs: `get_array` after an early TLAST at PF > 1 is stated only in a docstring;
  no example at a 256-bit internal link; no fixpoint conditional select; no throughput report step.

## Not now

- **The agent proposes the architecture** (the user's next direction): today a spec states the
  interfaces and partition in hardware terms; later the agent proposes them from the algorithm and a
  throughput target.  Recorded in `plans/guide_system_first.md`; no work on it until A-F are done.
- The memory-writer timing model under contention (rung 5's -6.4%): calibration work, its own plan.
