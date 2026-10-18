# Examples: order, legacy, and what needs work

A survey of `examples/` (2026-10-18), for the Flow Overview restructure (`plans/guide_system_first.md`
Stage 7).  Implemented by task C of `plans/next_docs_examples.md` -- see [Done](#done-task-c-2026-10-18).

## How they were classified

Each example's code was searched for the constructs of each flow -- `HostActivated` / `SeqTB` (the
host-launched flow, now under *Legacy flows*), `FreeRunMod` with `GraphSystem` (the free-running flow in
its older system form), `HwSystem(sim, dut)` / `harness` (the current form) -- and for its tests and the
pages that reference it.

## The order, and what each adds

Each row's objectives are only what it adds over the rows above it.

### The flow (the core path)

| # | example | adds | form today |
|---|---|---|---|
| 1 | **basic_vec** | schemas and fields (int, float, fixed); arrays and vectorized NumPy goldens; bit-exact against Vitis (csim of a plain function) -- data before any interface | csim; fine for what it is |
| 2 | **stream_kernel** | one free-running kernel on streams; the command-response protocol (status stream, fatal errors); golden from intents; framed ports; `run_iter`; the HLS body with lane routines; the harness; the system flow to `compare`; timed phases calibrated in the harness; then behind AXI-MM with a host, its timing composed | `HwSystem` / `harness` |
| 3 | **block_eq** | a composite: partitioning into stages that run concurrently; `StreamBroadcast`; internal framed links and their depth; one body alone and in the composite (`KernelTask(framed=)`); each stage verified in its own harness, then the whole | `harness` |
| 4 | **memcpy** | memory: a kernel that masters `m_axi` through `MemRStream` / `MemWStream`; a memory model in the testbench; adaptors whose timing ships pre-fit | `HwSystem` (`mem_copy_system()`) |
| 5 | **interleaver** | on-chip storage (blocks of BRAM, stream-of-blocks) between stages; a real compute stage; fitting a custom stage's own timing model | `HwSystem` (`interleaver_inband_system()`) |
| 6 | **fir_block** | state across firings (`add_state`): coefficients loaded once, a tail carried between blocks; fixed point; a four-module composite | `HwSystem` (`fir_block_system()`) |
| 7 | **bram_access** | memory outside the Vitis kernel: hand-written Verilog (`rtl_module`) joined by a generated wrapper; a true-dual-port memory shared by two tasks | `HwSystem` (`bram_access_system()`) |
| 8 | **mm_fir** | a kernel reached over the bus by a host program: a register bank and queues behind one adaptor, the crossbar; ordering a config against the data it applies to (a config id in-band); one front vs a slot per view | `HwSystem` |
| 9 | **markov** | several kernels on a bus: a credit stream routed over the crossbar, a shared memory, several bus masters, interrupts and no polling; timing composed from three calibrated modules | `HwSystem` |

Two candidates for new examples, both already specified and built blind (`evaluation/planning_tests/`):
**peak detect** (between 3 and 4: two stages on a wide link, a variable number of results as a tagged
union ending in END) and **packet capture** (after 9: two kernels, a memory and a host, a window copied
between them -- the hardest rung; its blind build passes every RTL gate).

### Measuring and exploring

| # | example | adds | form today |
|---|---|---|---|
| 10 | **vecmult** | resource modeling: a sweep over two parameters, a `VitisResourceModel` fitted from it | csim + csynth -- **needs work**: verify through the system flow, or keep it purely as the resource-model example |
| 11 | **xbar_profile** | measuring vendor IP into a platform's record store (a census, not a design) | measurement scripts |
| 12 | **mimo_cg** | the capstone: components (systolic multiply, CG vector unit) composed into a detector; bit-exact fixed point; a design-space study checked against a brute force | `harness` |

### Libraries and domains (after the core path, in any order)

| example | adds |
|---|---|
| **ssr_fft** | a full-rate FFT built from free-running modules; throughput vs resources |
| **vitis_fft** | wrapping vendor IP as a module; calibrating a black box; a cautionary tale about stated rates |
| **rf_loopback** -> **rf_shot_tx** -> **rf_shot_rx** -> **rf_shot_loopback** | the RF data converter boundary; playing and capturing through memory; measuring a delay by address |
| **cpu_sched** | software on a calibrated processor beside hardware |

### Legacy: host-activated (at the end, under *Legacy flows*)

| example | what it shows |
|---|---|
| **regmap** | a host-launched kernel over an AXI-Lite register map; csim / cosim |
| **stream_inband** | the host-launched form of the polynomial (the free-running form is stream_kernel) |
| **shared_mem** | a host-launched histogram reading and writing memory over `m_axi` |

## What needs work

- **`GraphSystem` -> `HwSystem(sim, dut)`**: the RF examples (rf_loopback, rf_shot_tx, rf_shot_rx;
  rf_shot_loopback has no system). memcpy, interleaver, fir_block and bram_access are done
  ([below](#graphsystem---hwsystemsim-dut-2026-10-18)).
- **vecmult**: its verification is csim, the legacy rung. Either move it to the system flow or present
  it only as the resource-model example (its own summary already says the arithmetic is beside the point).
- **stream_kernel / mm_fir overlap**: stream_kernel's *Behind AXI-MM* page already introduces a device
  and a host, so mm_fir's increment is the register bank, the config ordering and the front choice --
  its index should say so.
- **Learning objectives** in the form stream_kernel uses (testable skills, in flow order) on every curated
  example, stating only the increment over the examples before it.

**Keep every advanced example** (the user, 2026-10-18): the RF examples and components, the FFTs, mimo_cg,
cpu_sched, xbar_profile move later in the order, never out of the tree.  Only the unused directories
below are deleted.

## Outside the curated set

| dir | what it is | proposal |
|---|---|---|
| `bram_toy`, `test`, `vecunit` | no tracked files: local build leftovers | delete locally |
| `vscode` | one file (`myclass.py`), nothing references it | delete |
| `dse_fir` | one file (`fir_quality.py`), referenced only by a corpus test's list | delete, or fold into mimo_cg's study |
| `vmac` | the VMAC accelerator (21 files), superseded by the interleaver arc; two guide pages and two tests still reference it | archive: move the two pages' snippets to a current example, then delete |
| `_archive` | 70 files of old work; two tests reference it | delete (history keeps it) once the two tests are checked |
| `block_scale` | a sequential (`SeqTB`) hook demo, cited by `custom_hooks/block.md` | legacy guide demo: keep until that page moves to a free-running example |
| `toy`, `state_toy`, `interface`, `memory`, `schemas`, `timing` | small demos the guide pages run or cite (`add_state`, the XSI testbench, polling, memory, schema registry, timing diagrams) | keep as guide demos, not examples |
| `rf_blk_delay`, `rf_relayout`, `rf_repeat_play`, `rf_samp_buf_rx`, `rf_samp_buf_tx` | the RF components' worked material, tested, used by `guide/rf/` | keep; promote one to a curated page if the RF track needs it |
| `mcp_test` | blind-test fixtures | keep |

## Done (task C, 2026-10-18)

- **Order**: `nav_order` 1-20 on the curated indexes, in this file's order with `stream_kernel_mm` (task
  A) 4th, after block_eq: basic_vec, stream_kernel, block_eq, stream_kernel_mm, memcpy, interleaver,
  fir_block, bram_access, mm_fir, markov; vecmult, xbar_profile, mimo_cg; ssr_fft, vitis_fft,
  rf_loopback, rf_shot_tx, rf_shot_rx, rf_shot_loopback, cpu_sched.  The landing page
  (`docs/examples/index.md`) lists the four groups with a line each.
- **Legacy**: `docs/examples/legacy.md` (*Legacy examples*, nav_order 21) is the parent of regmap,
  stream_inband and shared_mem (`parent: Legacy examples`, `grand_parent: Examples`; their pages
  `grand_parent: Legacy examples`).  A file, not `docs/examples/legacy/index.md`: the corpus
  (`waveflow/mcp/knowledge/corpus.py::_toc_examples`) and `blind_test._toc_example_dirs` take every
  `docs/examples/<dir>/index.md` as an example card, and a parent page has no `example_dir:`.  The three
  directories did not move, so the cards and every link are unchanged.
- **Learning objectives** on every curated index, as the increment.
- **Deleted**: `examples/vscode`, `examples/dse_fir`, `examples/_archive` and their config references.
- **Not deleted -- `bram_toy`, `test`, `vecunit`** in the main checkout: zero tracked files (checked on
  `main`), but the deletion was refused by the agent's permission sandbox (outside its worktree).
  `vecunit/vecunit.py` and `test/dataschema2.ipynb` are hand-written untracked files; delete them by
  hand (`rm -rf examples/bram_toy examples/test examples/vecunit`).  Their `.gitignore` lines are gone,
  so until then they show as untracked.
- **Kept -- `vmac`**, for these reasons:
  1. `docs/guide/custom_hooks/complex.md` teaches *data-dependent addressing from the datapath*: a hook
     that walks an `m_axi` pointer itself (`read_array_lane` over a running word pointer, row strides
     from command fields, `read_array_slice` for a per-row scalar), and the two csynth gotchas that only
     arise there (scalar `*_core` arguments; `#pragma HLS INLINE` so `m_axi` binds to the top).  No
     current example has that shape: every memory-mastering kernel (memcpy, interleaver, fir_block,
     mimo_cg, the linalg tasks) reaches memory through `MemRStream` / `MemWStream` and computes on
     streams or stream-of-blocks; `read_array_lane` in the current examples (markov, mm_fir, the RF
     relayout tasks) reads stream words, not a memory pointer.  The complex datapath in
     `waveflow/linalg` (`systolic_*_task.h`, `cg_vector*_task.h`) matches only the page's second section
     (complex fixed point, a wide accumulator, one requantize), not its subject or its gotchas.
     Rewriting the page onto one of them would change what it teaches, not move it.
  2. The vmac tests are framework tests too: `tests/hw/test_extract_vmac.py` checks the extractor's
     `FunctionStmt` / `impl_file` path on `VmacAccel`, and `tests/examples/test_vmac_*.py` the
     instance-specialized command schema (`VmacAccel.Cmd`) that `waveflow/hw/hw_module.py` and
     `tests/hw/test_aximm_queue_stmt.py` cite.  Deleting vmac drops that coverage.
  What it would take: either retire the pattern (the guide now routes memory through the adaptors,
  and `m_axi` on an `hls::task` must be `offset=off` + `stable`), deleting complex.md and moving the
  extractor test onto a small fixture in `tests/hw/`; or build a current example with a strided /
  gathered `m_axi` datapath and move the page onto it.  vmac stays out of the corpus
  (`test_non_toc_example_directories_are_out_of_the_corpus`).
- **Not done**: the `GraphSystem` -> `HwSystem(sim, dut)` migrations (memcpy, interleaver, fir_block,
  bram_access, the RF examples), left for its own pass -- since taken up, see
  [below](#graphsystem---hwsystemsim-dut-2026-10-18).  vecmult is placed as the resource-model example, without an RTL rung.
- `examples/` is an installed package: after these deletions re-install (`pip install -e .`) so the
  dropped `examples._archive*` exclude leaves the metadata.

## `GraphSystem` -> `HwSystem(sim, dut)` (2026-10-18)

The pattern, one example per commit: the testbench composite's wiring moves into a function that
builds the kernel and its participants on a given simulation (`wire_<example>(sim, ...)`, same
creation order and names, so pysim's event order is unchanged); the composite (`<Example>TB`, kept
for the calibration tools, the kernel-flow testbench and `tb_top_spec`) registers those parts as its
children; the system is a function, `<example>_system(...)`, that builds the parts on a fresh
`Simulation`, wraps the kernel in `DutTop.of("<kernel>_dut", ...)` and returns
`HwSystem(sim, dut, n_cycles=, inputs=, check=, fresh=)` with the parts as attributes.  The build
modules pass their `TOP` explicitly, so the RTL top keeps its name (only `SystemTopSpec.from_system`
without `top=` now defaults to the DUT's name).  Gate: a pysim probe (scenario + run hashed, each
sink burst's arrival cycle, the end time) identical before and after, and the `-m xsi` gates
re-run on fresh RTL (Vitis/Vivado 2025.1).

| example | pysim (before = after) | RTL (before = after) |
|---|---|---|
| memcpy | 16 completions, last at 2361 cycles; scenario + recordings hash identical | 2908 (`test_mem_copy_system_xsi`, `test_mem_copy_unit_xsi`) |
| interleaver | 12 completions, last at 1110 cycles; hash identical | 1300 (`test_interleaver_inband_xsi`) |
| fir_block | 5 completions, last at 486 cycles; hash identical | completions at 67 / 164 / 275 / 323 / 420, arena identical (`test_fir_block_xsi`, no cycle assert) |
| bram_access | zero: 25 sink bursts, last at 531 cycles; collision: same log and hash as before; both hashes identical | zero and collision: every sink's arrival cycles identical (resp_w 266..476, data_r 274..568, resp_r 274..569; collision resp_w ..1083); `test_bram_access_xsi` 12 passed |
