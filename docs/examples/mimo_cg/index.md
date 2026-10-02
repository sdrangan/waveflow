---
title: CG massive-MIMO detector
parent: Examples
nav_order: 11
summary: "A conjugate-gradient MMSE detector for the massive-MIMO uplink, taken from link-level BER to bit-exact fixed point to synthesized, RTL-verified hardware on the RFSoC xczu48dr — one Python model throughout. The accuracy design space is explored without Vitis; Vitis is spent only on hardware cost."
---

# CG massive-MIMO detector: what is built so far

The uplink base station has M antennas and serves K users. It must solve `(HᴴH + σ²I) x = Hᴴy` for
every received vector. Conjugate gradient (CG) is the low-complexity alternative to a direct solve,
and its iteration count is a natural accuracy-versus-latency knob. This study uses Waveflow to find
the cheapest hardware that meets an accuracy target. The bit-exact Python model answers the accuracy
side exactly and with no Vitis. Calibrated models will answer the cost side, with Vitis runs only
where they are needed.

**Status:** phases 0–4 are complete and reviewed (milestones M0–M4). Next are the performance models
(Phase 5) and the full design-space exploration (Phase 6). The plan, with every decision and its
evidence, is `plans/mimo_cg/mimo_cg_paper_sims.md` on branch `paper/mimo-cg`.

![The study](images/diagram_study.svg)

### Results at a glance

| Question | Result so far |
|---|---|
| How many CG iterations? | 2–4 when M/K ≥ 8, 3–6 at M/K = 4, 6–12 at M/K = 2 (float CG within 0.5 dB of exact MMSE) |
| How narrow can the datapath be? | 8–10 bits for QPSK, 10–12 for 16-QAM, 12–14 for 64-QAM, with 8 guard bits on two scalars |
| What do guard bits buy? | 2–6 bits (median 4) on every register; without them 64-QAM at 32×16 fails at every W ≤ 20 |
| Does quantization add iterations? | No: in all 27 configurations the narrowest W works at float CG's own iteration count |
| Does the hardware meet 4 ns? | Yes: every unit and the integrated detector at 3.35–3.39 ns (est.), K = 4, 8, 16 |
| What does it cost? | Detector: 112 / 176 / 304 DSP (2.6–7.1% of the xczu48dr) at K = 4 / 8 / 16 |
| How fast is it? | 1,193 / 1,449 / 1,969 cycles per CG iteration for a block of 32 vectors at K = 4 / 8 / 16 |
| Is it right? | The RTL output matches the Python golden bit for bit: K = 4, 8, 16 at every iteration count, and both frontier formats at K = 4 |

## 1. The algorithm, as hardware sees it

> **Files** (in `examples/mimo_cg/`): `mimo_cg_fixed.py` (the bit-exact golden), `cpp/cg_ref.h`
> (the C++ reference); `ap_fixed` arithmetic in `waveflow/utils/fixputils.py`.

Multi-RHS CG solves for all N = 32 vectors of a coherence block at once, with a separate α and β per
column. In fixed point, every product and sum between registers is exact. The only rounding is the
assignment to a register (`ap_fixed`, round to nearest with saturation), so a Python model built on
integers reproduces Vitis bit for bit. The loop splits into two blocks: a matrix multiply (step 1)
and a vector unit (steps 2–9). The Python golden is split the same way (`mm_step`, `vec_step`).

![One CG iteration](images/diagram_cg_iteration.svg)

- **Division** is native `ap_fixed` division, modelled bit-exactly in `waveflow/utils/fixputils.py`,
  with a zero guard (α = 0 when pᴴAp = 0, β = 0 when rᴴr = 0).
- **Guard bits** (g_s) widen only the two quadratic forms, pᴴAp and rᴴr. Phase 3 showed that this is
  where precision runs out.

## 2. Accuracy, with no Vitis (phases 1–3)

> **Files** (in `examples/mimo_cg/`): `mimo_link.py` (link and BER), `detectors.py` (ZF, MMSE,
> float CG), `mimo_cg.py` and `mimo_cg_build.py` (Phase 1), `mimo_cg_accuracy_sweep.py`,
> `mimo_cg_accuracy_analysis.py` and `mimo_cg_accuracy_figures.py` (Phase 3); data in `paper_data/`.

Phase 1 swept the floating-point link: M ∈ {32, 64, 128} × K ∈ {4, 8, 16} × QPSK, 16-QAM and 64-QAM,
for ZF, exact MMSE and CG. Simulated ZF lands on its closed-form BER (the × marks), which checks
the simulator against theory. CG reaches exact MMSE in a few iterations when M/K is large. At
M/K = 2 it needs many more, and too few iterations leave an error floor.

![Floating-point BER, 16-QAM](images/float_ber_16qam.svg)

Phase 3 ran the bit-exact detector on every configuration: 364 points × 21 formats, with W from 8
to 20 bits and g_s ∈ {0, 4, 8}. Every format saw the same channels, symbols and noise as the
floating-point references. The decision points were refined to 1000 errors, which gives about 0.03 dB
of loss resolution. A design meets the budget if it loses at most 0.5 dB against floating-point exact
MMSE at BER 1e-3.

**One configuration in detail: 64×8 16-QAM.** At 8 iterations, W = 12 without guard bits loses
2.4 dB and flattens out near BER 2e-4. With 4 or 8 guard bits it tracks floating point (below). The loss map for every
(W, iterations) pair shows the cheapest design within budget: W = 10, g_s = 8, 3 iterations, which
loses 0.32 dB.

![BER of fixed-point CG, 64×8 16-QAM](images/accuracy_ber_64x8_16qam.svg)

![SNR loss map, 64×8 16-QAM](images/accuracy_loss_64x8_16qam.svg)

**The hardest configuration: 32×16 64-QAM.** No register width up to 20 bits meets the budget
without guard bits. With 8 guard bits, W = 14 works at 12 iterations.

![SNR loss map, 32×16 64-QAM](images/accuracy_loss_32x16_64qam.svg)

**All 27 configurations.** The narrowest width for each guard setting (first figure below), and
the narrowest design at each iteration count (second).

![Narrowest width per guard setting](images/result_guard_savings.svg)

![Narrowest register width within 0.5 dB](images/accuracy_min_width.svg)

- **Guard bits pay most of the width.** With 8 guard bits on just the two quadratic forms, the
  narrowest register is 8–10 bits for QPSK, 10–12 for 16-QAM and 12–14 for 64-QAM. Without guard bits
  it is 12–16, 14–18 and 16–18 bits. That saves 2–6 bits (median 4) on every vector and matrix
  register.
- **Quantization does not add iterations.** The narrowest width always works at floating-point CG's
  own iteration count: 2–4 iterations when M/K ≥ 8, 3–6 at M/K = 4 and 6–12 at M/K = 2.
- **Extra iterations can hurt in fixed point.** 384 designs meet the budget at some iteration
  count. Running longer costs 23 of them more than 1 dB, or drives them into an error floor; 22 of
  the 23 are at K = 16. Guard bits do not prevent it, so hardware should stop at the frontier's count.
- **Implication for hardware:** a 12-bit datapath with 8 guard bits covers every configuration except
  64-QAM at 32×16, which needs 14 bits.

## 3. The hardware (Phase 4)

> **Files** (in `examples/mimo_cg/hw/`): `common.py` (types, commands, memory format), `vec.py`,
> `mm.py`, `detector.py` (the modules), `cpp/*.h` (the HLS bodies), `build.py` (codegen, csynth,
> XSI).

The detector is a free-running composite of `hls::task`s. It sits on the same framework memory
streams as the other examples, and its parts map onto the paper's fixed architecture:

- a **systolic matrix multiply**;
- a **vector unit**;
- **shared memory**: stream-of-blocks between the tasks;
- **queues**: command FIFOs from a separate **CG control** task.

All of these are Phase 5 design-space knobs, along with the lane count L, the array size R × C, the
complex-multiply form and the queue and block depths.

![The integrated detector](images/diagram_detector.svg)

![The systolic array](images/diagram_systolic.svg)

### How each module is built

Every module has two halves:

- **A Python model.** A `FreeRunMod` subclass declares the module's ports and parameters. Its
  body calls the bit-exact golden, so the Waveflow simulation checks how the modules connect,
  their commands and their memory layout. Its timing is a placeholder until Phase 5 calibrates it.
- **An HLS body.** A hand-written `hls::task` function in `examples/mimo_cg/hw/cpp/`. Each model's
  `kernel_task()` names its body and template arguments.

The top-level C++, the synthesis script and the RTL testbench are not written by hand. They are
generated from the Python graph (more under "Generated, not hand-written" below).

The detector's structure is not new. It follows the in-band interleaver
(`examples/interleaver/interleaver_inband.py`): a framer splits each host command into memory
reads, the framework memory reader fetches the data, a load task unpacks it into stream-of-blocks,
the compute runs, a store task packs the result, and the framework memory writer writes it and
signals done. The CG design keeps that skeleton and replaces the compute with the CG core. The
generated K = 4 top shows all eight tasks:

```cpp
hls_thread_local hls::task t0(cg_cmd_rx_task<64, 4, 32>, s_cmd, cmd_rd);
hls_thread_local hls::task t1(mem_r_stream_framed_task<64>, cmd_rd, m_in, rdata);
hls_thread_local hls::task t2(cg_load_task<64, 4, 32, 4, 2>, rdata, desc_lc, a_blk, b_blk);
hls_thread_local hls::task t3(cg_ctrl_task<64, 4>, desc_lc, vec_q, mm_q, desc_cs);
hls_thread_local hls::task t4(cg_vec_task<64, 4, 32, 4, 2>, vec_q, b_blk, s_blk, p_blk, x_blk);
hls_thread_local hls::task t5(cg_mm_task<64, 4, 32, 4, 4, 4, 4, 2>, mm_q, a_blk, p_blk, s_blk);
hls_thread_local hls::task t6(cg_store_task<64, 4, 32, 4, 2>, desc_cs, x_blk, wdata);
hls_thread_local hls::task t7(mem_w_stream_framed_done_task<64, 8>, wdata, m_out, s_done);
```

| Task | What it does | Python model | HLS body | New or reused |
|---|---|---|---|---|
| `cg_cmd_rx` | Turns one host command `CgCmd {a_off, b_off, x_off, nit}` into two framed reads, A then B. The A read carries the job's descriptor `CgDesc {nit, x_off}` ahead of its data. Clamps nit to 1…K. | `CgCmdRx`, `hw/detector.py` | `hw/cpp/cg_cmd_rx_task.h` | New; same role as the interleaver's `CmdRxInband` |
| `MemRStream` (in-band) | AXI-MM burst reader on `m_in` (`gmem0`): two reads per job | `waveflow/hw/mem_stream.py` | `waveflow/build/mem_r_stream_framed_task.h` | Reused unchanged: the framework reader used by `mem_copy` and the interleaver |
| `cg_load` | Converts memory words to registers. Lands A as K rows for the matmul (`a_blk`) and B as lane groups for the vector unit (`b_blk`), and passes the descriptor to control | `CgLoad`, `hw/detector.py` | `hw/cpp/cg_load_task.h`, `cg_io.h` | New; same role as `IlLoadInband` |
| `cg_ctrl` | **CG control.** For each job, puts INIT and then nit commands (ITER … LAST) into each block's queue, then passes the descriptor to the store | `CgCtrl`, `hw/detector.py` | `hw/cpp/cg_ctrl_task.h` | New |
| `cg_vec` | **Vector unit**, CG steps 2–9 and the start | `CgVec`, `hw/vec.py` | `hw/cpp/cg_vec_task.h` | New (see below) |
| `cg_mm` | **Systolic matrix multiply**, CG step 1: S = q_S(A·P) | `CgMm`, `hw/mm.py` | `hw/cpp/cg_mm_task.h` | New (see below) |
| `cg_store` | Writes X, then a zero-length write that carries the descriptor, so reads and writes balance (two of each per job, section 5) | `CgStore`, `hw/detector.py` | `hw/cpp/cg_store_task.h` | New; same role as `IlStoreInband`, plus the second write |
| `MemWStream` (in-band, done) | AXI-MM burst writer on `m_out` (`gmem1`); signals one done per job on `s_done` | `waveflow/hw/mem_stream.py` | `waveflow/build/mem_w_stream_framed_done_task.h` | Reused unchanged |

Notes on the two compute blocks:

- **Vector unit (`cg_vec`).** Its arithmetic is transcribed from the C++ reference
  `examples/mimo_cg/cpp/cg_ref.h`, which was already proven bit-exact against the Python golden in
  Phase 2.
  - **State.** It holds X, R, P and rᴴr for the whole job on chip. The arrays are split across L
    lanes (default 4), so L columns are processed side by side.
  - **One iteration.** For each group of L columns it makes three pipelined passes (one row per
    cycle) over the K rows:
    1. pᴴAp, then α from L dividers working in parallel;
    2. the X and R updates and the new rᴴr, then β;
    3. the P update.
  - **Rounding.** Every accumulator is declared at its exact width, so the only rounding happens
    when a value is stored in a register, just as in the golden.
  - **Why not the existing vector engine.** The repo's `examples/vmac/` was evaluated at gate 4.0
    and not reused. Its datapath has a single number format, scales one α per row instead of per
    column, and has no divider, so it does not match the golden.
- **Matrix multiply (`cg_mm`).** A new systolic array of R × C complex processing elements
  (default R = K, C = 4), sketched in the diagram above.
  - **Data flow.** A is held for the whole job, and a new P arrives each iteration. A values shift
    right and P values shift down, with the usual skew.
  - **Accumulation.** Each element accumulates its S entry exactly and rounds once, so the result
    is bit-exact. S is computed in (K/R)·(N/C) tiles.
  - **Complex multiply.** `cmul` = 4 uses the standard product; `cmul` = 3 uses the Gauss form,
    which needs one multiplier fewer per element and is still exact.
  - **Why not a library GEMV.** The Vitis library GEMV (`vitis_l1`) handles only real numbers, so
    it was not used.

Shared pieces:

- **Shared memory.** The blocks `a_blk`, `b_blk`, `p_blk`, `s_blk` and `x_blk` are
  `hls::stream_of_blocks` ping-pong buffers, `sob_depth` blocks deep (default 2). They are declared
  in Python as framework `StreamOfBlocksIF` connections. Each element is a **lane group**: L complex
  values packed into one `ap_uint<2·W·L>` word (`hw/cpp/cg_lanes.h`), for example 96 bits for
  W = 12, L = 4.
- **Queues.** These are framework `StreamIF` FIFOs, `cmd_depth` deep (default 2). They carry
  `CgIterCmd {op: INIT | ITER | LAST}`.
- **Command types.** `CgCmd`, `CgDesc` and `CgIterCmd` are Waveflow `DataSchema` types in
  `hw/common.py`, and their C++ headers are generated from them.
- **Memory format.** In memory, each value is widened to 16 bits per real or imaginary part, which
  is exact, so a complex element fills an aligned 32 bits. The packing uses the framework's
  `DataArray[ComplexField[FixedField]]` serialization and its generated C++ array utilities, so no
  task hand-codes shifts or masks.
- **Register types.** The `ap_fixed` register types in `cg_types.h` are generated from the Python
  formats by `render_typedefs` (`hw/common.py`). The Phase 2 conformance test uses the same
  function, so the hardware and the C++ reference always agree on types.

**Per-block test units.** `CgVecUnit` (`hw/vec.py`) and `CgMmUnit` (`hw/mm.py`) each wrap one block
with its own framer, load and store tasks (`hw/cpp/cg_vec_{rx,load,store}_task.h`,
`hw/cpp/cg_mm_{rx,load,store}_task.h`), so the block can run on its own from memory. They are used
for the per-block RTL checks, and in Phase 5 they will supply the per-block calibration. The extra
load and store tasks are also why the per-block LUT and BRAM counts do not add up to the
detector's.

**Generated, not hand-written.** For each configuration, `hw/build.py` uses the framework's
`waveflow/build/composite_gen.py` to write the build products into
`examples/mimo_cg/hw/build/<top>_<config>/` (gitignored):

- the `ap_ctrl_none` top-level C++, walked from the Python graph (`composite_top_spec`,
  `render_top`);
- the csynth script for xczu48dr at 4 ns (`render_tcl`);
- the XSI testbench harness (`tb_top_spec`, `render_tb_harness`), which drives the RTL with the same
  scenario as the Python simulation and runs through the repo's XSI runner (`xsi_runner_cmd`).

C-sim uses `hw/csim.py`, which calls the task bodies in order rather than as threads (section 5
explains why).

### Synthesis results

csynth on `xczu48dr-ffvg1517-2-e`, 4 ns target, W = 12 bits with 8 guard bits:

![Synthesized resources](images/result_hw_resources.svg)

| Build | K | Est. clock | DSP | BRAM_18K | LUT |
|---|---|---|---|---|---|
| Vector unit (L = 4) | 4 / 8 / 16 | 3.35 ns | 48 | 20 / 20 / 44 | 29.7k / 30.2k / 30.2k |
| Matmul (R = K, C = 4), 4 multiplies | 4 / 8 / 16 | 3.35 ns | 64 / 128 / 256 | 14 / 20 / 33 | 19.1k / 27.4k / 46.5k |
| Matmul, 3 multiplies (Gauss) | 4 / 8 | 3.39 ns | 48 / 96 | 14 / 20 | 18.9k / 27.1k |
| Integrated detector | 4 / 8 / 16 | 3.35 ns | 112 / 176 / 304 | 20 / 26 / 63 | 33.0k / 41.6k / 60.8k |

The detector's DSPs are exactly the two blocks' sum. LUTs and BRAM are not, because each per-block
unit carries its own load and store tasks. Phase 5 will calibrate those from the per-task rows of
the report.

## 4. Verification: one golden, all bit-exact

> **Files:** `examples/mimo_cg/mimo_cg_conformance.py` (golden vs C++ reference),
> `examples/mimo_cg/hw/csim.py` (C-sim), `examples/mimo_cg/hw/build.py` (XSI run and check); tests
> in `tests/examples/test_mimo_cg_*.py`.

![Verification ladder](images/diagram_verification.svg)

At RTL, the integrated detector reproduces `cg_fixed` word for word:

- at K = 4 on problems from M = 32 channels (AC4's check), for both W12g8 and W14g8;
- at K = 8 and 16;
- at every iteration count up to K.

The same RTL runs time the detector. Jobs were issued back to back, and the gap between
consecutive job completions grows linearly with the job's iteration count. The fitted lines below
match every measured point to within one cycle:

- each CG iteration on a block of N = 32 vectors costs 1,193, 1,449 and 1,969 cycles at
  K = 4, 8 and 16 (about 4.8, 5.8 and 7.9 µs at 250 MHz);
- the per-job overhead is 75–267 cycles.

For example, the 64×8 16-QAM headline design runs 3 iterations. On the K = 8 build that is
139 + 3 × 1,449 = 4,486 cycles, about 18 µs per block of 32 vectors.

The right panel shows the divider fix (section 5): the same 20-job run went from 161k to 61k
cycles.

![RTL cycles per job](images/result_rtl_cycles.svg)

## 5. What the hardware work taught us

> **Files:** `examples/mimo_cg/hw/cpp/cg_store_task.h` (balanced writes),
> `examples/mimo_cg/hw/cpp/cg_vec_task.h` (safe divisor), `examples/mimo_cg/hw/csim.py` (sequential
> C-sim); write-ups in `plans/mimo_cg/mimo_cg_lessons.md`.

![The reads-equal-writes rule](images/diagram_deadlock.svg)

- **Reads and writes must balance per job** (above). The bug was invisible in the Python simulation
  and in C-sim, and appeared only at RTL after six jobs.
- **A guarded divide is a serial divide.** `(d == 0) ? 0 : n / d` in an unrolled loop made HLS
  run the vector unit's dividers one after another. Dividing by a safe divisor and then selecting is
  bit-exact, and made the detector 2.6× faster.
- **A threaded C-sim of stream-of-blocks races** in Vitis 2024.1: its model hands a block to the
  reader before the writer has filled it. C-sim here therefore fires the task bodies in dependency
  order; RTL has the real ping-pong semantics.

## 6. Next

> **Files:** none yet; phases 5 and 6 are specified in `plans/mimo_cg/mimo_cg_paper_sims.md`.

- **Phase 5:** per-block cycle and resource models, calibrated from a small set of syntheses and
  XSI runs. A hold-out split is fixed before any fitting.
- **Phase 6:** sweep the whole cross-product in Python (accuracy, DSP, LUT, BRAM, latency) to get the
  Pareto frontier, and check it against a brute-force Vitis subset.

## Where things are

| What | Where |
|---|---|
| Link simulator, detectors, floating-point sweep | `examples/mimo_cg/mimo_link.py`, `detectors.py`, `mimo_cg.py` |
| Bit-exact golden | `examples/mimo_cg/mimo_cg_fixed.py` (C++ reference: `examples/mimo_cg/cpp/cg_ref.h`) |
| Accuracy sweep and analysis | `examples/mimo_cg/mimo_cg_accuracy_sweep.py`, `mimo_cg_accuracy_analysis.py` |
| Hardware blocks, codegen, C-sim | `examples/mimo_cg/hw/` (`vec.py`, `mm.py`, `detector.py`, `build.py`, `csim.py`, `cpp/`) |
| Tests | `tests/examples/test_mimo_cg_*.py`: no markers for the fast checks, `-m vitis` for C-sim and csynth, `-m xsi` for RTL |
| Paper data | `examples/mimo_cg/paper_data/*.csv` |

The concept diagrams come from `python docs/examples/mimo_cg/make_diagrams.py`. The `result_*`
plots come from `python docs/examples/mimo_cg/make_results.py`. That script reads the Phase 3
tables and the Phase 4 csynth reports and XSI cycle logs, and snapshots the hardware numbers to
`hw_csynth.csv` and `hw_rtl_cycles.csv` (next to the script) so the plots regenerate without
Vitis. The `float_*` and
`accuracy_*` figures come from the example's own build commands.
