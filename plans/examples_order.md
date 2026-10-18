# Examples: order, legacy, and what needs work

A survey of `examples/` (2026-10-18), for the Flow Overview restructure (`plans/guide_system_first.md`
Stage 7).  Proposal for the user's review; nothing has moved.

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
| 4 | **memcpy** | memory: a kernel that masters `m_axi` through `MemRStream` / `MemWStream`; a memory model in the testbench; adaptors whose timing ships pre-fit | `GraphSystem` -- migrate |
| 5 | **interleaver** | on-chip storage (blocks of BRAM, stream-of-blocks) between stages; a real compute stage; fitting a custom stage's own timing model | `GraphSystem` -- migrate |
| 6 | **fir_block** | state across firings (`add_state`): coefficients loaded once, a tail carried between blocks; fixed point; a four-module composite | `GraphSystem` -- migrate |
| 7 | **bram_access** | memory outside the Vitis kernel: hand-written Verilog (`rtl_module`) joined by a generated wrapper; a true-dual-port memory shared by two tasks | `GraphSystem` -- migrate |
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

- **`GraphSystem` -> `HwSystem(sim, dut)`**: memcpy, interleaver, fir_block, bram_access, and the RF
  examples (rf_loopback, rf_shot_tx, rf_shot_rx; rf_shot_loopback has no system). The plan's follow-on
  item, one example at a time; memcpy first, since it is 4th in the order.
- **vecmult**: its verification is csim, the legacy rung. Either move it to the system flow or present
  it only as the resource-model example (its own summary already says the arithmetic is beside the point).
- **stream_kernel / mm_fir overlap**: stream_kernel's *Behind AXI-MM* page already introduces a device
  and a host, so mm_fir's increment is the register bank, the config ordering and the front choice --
  its index should say so.
- **Learning objectives** in the form stream_kernel uses (testable skills, in flow order) on every curated
  example, stating only the increment over the examples before it.

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
