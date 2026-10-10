# Waveflow DSE paper — vision notes (CG matrix inverse)

**Status: VISION NOTES, not yet a plan.** Written 2026-07-04, revised 2026-10-10 after the
`mcp_frames` blind tests and the timing instrumentation (PR #244). The north-star paper that ties
the program together. Most of its infrastructure now exists (see the build-vs-have map); "Path to
a plan" at the end lists what turns these notes into staged work.

## Thesis / contribution

A **single Python source** that is simultaneously:
1. a **bit-exact functional model** — so *accuracy* design-space exploration (DSE) is
   **exact** and fast (no Vitis in the loop), and
2. a **calibrated cycle- and resource-*approximate* model** — so *performance* DSE is
   fast,

with **full Vitis used only for calibration + sparse validation**. The asymmetry is
the honest framing: **exact** accuracy, **approximate** performance. Headline result:
*explore N design points with K ≪ N full Vitis runs, and show the DSE conclusions
match brute-force-Vitis ground truth on a held-out subset.*

### Why this holds when code is cheap

AI makes *writing* hardware code cheap. A with/without-Waveflow blind test (Oct 2026, a
two-kernel bus system) showed it plainly: an agent with no framework hand-wrote a crossbar, its
slave ports, two HLS kernels and an RTL testbench, and reached a bit-exact RTL simulation with
fewer tokens than the agent using Waveflow. A contribution that rests on saving coding effort
does not survive that.

This one does not rest on it. AI does nothing to make *evaluating* a design cheap, and the cost
of evaluation is what bounds a design search. Measured on the examples
(`docs/guide/build/timing_events.md`):

| evaluation | cost per point |
| --- | --- |
| pysim of a whole system | ~0.1 s |
| csynth | 20–60 s per top |
| XSI from scratch | 20–30 s (≈1% of it simulating) |
| Vitis cosim | ~3 min (a harness rebuilt every run) |

An agent writing from scratch pays the right-hand column at every design point. The thesis is
about not paying it, and about being able to *trust* the cheap answer: exact accuracy by
construction, performance with a stated, validated error. Lead with that, not with
productivity.

## Positioning

- **vs HLS-DSE.** Existing work either puts the **HLS tool in the loop** (accurate but slow —
  what we avoid) or uses **pure analytical models** (fast but not functionally exact).
  Waveflow's angle is the **combination from one source**, with a calibration method that says
  when the approximate model can be believed.
- **vs LLM-for-hardware.** Most of that work asks whether a model can *write* RTL that passes a
  testbench. Our blind tests suggest that bar is close to met for small designs. The open
  problems are **trusting** the result (an implementation checked by a testbench from the same
  author is self-consistent, not verified) and **exploring** the design space at a cost a search
  can afford. This paper addresses both; it is complementary to code generation, not in
  competition with it.

## The vehicle: conjugate-gradient matrix inverse (wireless)

High-value: massive-MIMO detection needs `A⁻¹` (or `A⁻¹b`) for the regularized Gram
matrix `A = HᴴH + σ²I`; CG is the low-complexity iterative alternative to O(n³)
Cholesky. Genuinely iterative → **#iterations is a first-class accuracy↔latency knob**.
Decomposes cleanly into matmul + vector ops over shared memory.

- **Accuracy metric: BER/MSE-vs-SNR** (the real link curve), not just CG residual
  ‖Ax−b‖. The bit-exact model is what makes that curve *trustworthy* (true fixed-point
  behavior, not a float approximation).
- **Block-CG** (solve `AX=I`, multiple columns/RHS at once) makes the inner op
  matrix-*matrix*, which **justifies the systolic array** (plain CG's matrix-*vector*
  needs only a MAC array) and stresses the memory-width/queue knobs harder.

## The fixed architecture (the smart scoping move)

Not "explore all architectures" — explore the **parameters of one fixed architecture**:
- **Systolic array** — matrix multiplication (block-CG matmul).
- **Vector unit** — column-wise ops (CG dots for α/β, AXPY updates of x/r/p).
- **Shared memory + queue** — CG state exchange between the two blocks.
- **CG control** — the iteration loop tying them together.

In today's terms this is a **`bus_system`** (`docs/guide/ai_tooling/frames.md`): free-running
kernels and an on-chip memory on one crossbar, command-response jobs, a host that never polls —
the shape `markov` proves, verified by the system DAG and its trace gate. That frame's rules
(stream-only kernels, credit on routed links, addresses from `assign_address_ranges`) apply as
written.

## Parameters & metrics

- **Parameters:** bit widths (accuracy↔DSP), memory-access width (throughput↔BRAM/
  routing), queue sizes (stall behavior), #CG iterations (accuracy↔latency), array
  size.
- **Metrics:** accuracy (BER/MSE), resources (DSP/BRAM/LUT/FF), throughput/latency.

### Design parameters vs workload parameters

The parameters do not all cost the same at RTL, and the brute-force baseline must count them
honestly:

- **Design parameters** — bit widths, array size, memory width, queue depths — change the RTL.
  Each new point pays csynth of the changed blocks, then elaboration: minutes. **The K ≪ N
  claim lives here.**
- **Workload parameters** — make **#iterations** a field of the CG command, not a build
  parameter. Then sweeping it needs no new RTL: with an incremental XSI runner
  (`plans/incremental_xsi.md`), each point costs the simulation alone. Its *accuracy* sweep
  needs no performance model at all. Claiming a speedup over Vitis on this axis would be
  claiming against a straw man.

## The two-model approach

- **Accuracy = exact.** The bit-exact functional model (`FixedField`/`ComplexField`)
  reproduces the fixed-point hardware bit-for-bit, *proven* by the conformance harness.
  So accuracy sweeps over bit width / iterations are exact and need **no Vitis**.
- **Performance = approximate + calibrated.** Cycle and resource estimates from
  calibrated models; full Vitis only to calibrate + spot-validate.

## Resource-model methodology (the active-calibration mechanism)

This is the principled mechanism behind "limits the number of full Vitis sims" — it
turns the claim from "we ran fewer" into "here's *why* fewer provably suffice."

**Reframe:** retraining a small model (GP / ridge / small ensemble) on tens-to-hundreds
of points is ~free — do it every new point. **The cost is the full synthesis.** So the
optimization is *minimize syntheses while keeping predictions accurate enough to not
change the DSE decision.*

**Biggest lever — per-block models turn a combinatorial space into an additive one.**
The fixed, memory-decoupled blocks let you:
- model **each block's** resources from **its own few parameters** (low-dim → few
  points each);
- **synthesize blocks independently** (systolic array alone over sizes/widths; vector
  unit alone; …) — cheaper and cleaner attribution than full-design synthesis;
- full-design estimate = **Σ block predictions + a small integration-overhead term**
  calibrated from a *handful* of full-design syntheses.

→ You synthesize **O(Σ per-block parameter ranges)** (≈ linear per parameter) and
**predict the entire cross-product** from the summed block models. You never synthesize
the cross-product.

This composition now exists in the resource model (`docs/guide/resource_model/`): every module
has a model, a composite's estimate is its own interface term plus the sum of its children's,
and every prediction carries a confidence. What the paper adds is the *active* part below —
when to spend a synthesis — and its validation.

**Don't learn known physics — analytical prior + learned residual.** Per (resource ×
block):
- **DSP** ≈ multiplier-count × DSP-packing(bit width) — a *known step function*; encode
  it, learn a small correction.
- **BRAM** ≈ `ceil(depth·width / block_size)` with 18K/36K granularity — known step
  function; encode it.
- **LUT/FF** — the genuinely *learned* part (control, glue, vector-unit logic).
Encoding the **step discontinuities** also fixes a real failure mode: smooth models
(GPs) predict block-granularity jumps poorly; learn the *smooth residual* on top of the
analytical steps.

**When to spend a synthesis — uncertainty- and decision-aware sampling.** Use a model
with uncertainty (GP / ensemble variance):
- trust it *inside* the convex hull of sampled points; synthesize when *extrapolating*
  outside it;
- **decision-aware:** accurate resources only matter near the accuracy/throughput/
  resource **Pareto frontier** (where error changes *which design you'd pick*) — bias
  synthesis there, skip dominated regions;
- **error-triggered densification:** a prediction off by > tolerance → sample denser
  there + refit; accurate → sample sparsely.

**Stopping — decision convergence, not error→0.** Stop when the **Pareto frontier /
selected designs stabilize** (more syntheses stop changing the chosen points) — a
stronger, more honest claim than a generic regression error (you'll never drive LUT/FF
error to zero).

**Caveat (what the full-design syntheses guard):** per-block + integration-term
composition assumes block resources are roughly **additive** — true when synthesis
doesn't share/optimize aggressively across block boundaries (usually so for a fixed,
modular, memory-decoupled architecture — another reason the fixed two-block + shared-
memory choice is good). The handful of full-design runs catch any cross-block surprise.

## Cycle model (same calibrate-from-runs spine)

Cycles are more tractable than resources: analytically modelable (II × loop bounds +
burst transfer + queue stalls) and calibrated per block from RTL runs. CG cycles ≈
#iters × (matmul + vector + memory + queue-stall) per-block cycles. Same per-block,
calibrate-from-runs structure as the resource model.

**Calibrate with XSI, not cosim.** A cosim run carries ~3 min of fixed cost (its testbench
harness is regenerated and re-elaborated every run); an XSI run of a free-running block is
20–30 s from scratch, and should be ~simulation time once the runner reuses its snapshot
(`plans/incremental_xsi.md`). The evidence that the calibrated pysim tracks RTL already exists
for the system flow: `markov` within 3.3%, `mm_fir` 4.4%, the blind-test `scale_sum` 1.7–4%,
`memcpy`'s per-job period within 3%.

## Experimental structure

1. **Calibrate** — per-block syntheses and XSI runs to fit the resource + cycle models.
2. **Validate** — held-out design points: show predicted vs actual cycles/resources are
   accurate *across the space*, not just at calibration points. (This is the make-or-
   break rigor.)
3. **DSE** — sweep the full parameter cross-product in Python (exact accuracy +
   predicted performance); produce the accuracy/resource/throughput Pareto frontier.
4. **Baseline + finding** — (a) quantify the win: brute-force Vitis at every *design* point
   (csynth + RTL, counted per the design/workload split above) = X compute-days vs Waveflow =
   Y minutes + K calibration runs, conclusions matching ground truth on the validation subset;
   (b) a concrete **design finding** (e.g. "12-bit + 8 iterations hits target BER at half the
   DSPs of naive 16-bit/12-iter").
5. **Optional — an agent in the loop.** Keep the core result agent-free: an agent adds variance
   and would confound the method. As a separate experiment, give an agent the DSE task with and
   without the calibrated model, using the blind-test harness (`docs/guide/ai_tooling/blind.md`)
   and the timing events. Measure how many RTL evaluations each spends, and whether each reaches
   the right Pareto points. This speaks directly to the LLM-for-hardware audience.

Every evaluation in steps 1–5 is a timing span (`waveflow.events`), so the cost side of the
baseline comes from the runs' own logs (`analyze_events`), not from estimates.

## Reviewer risks / make-or-break

1. **Approximate-model validation** is the whole ballgame — *held-out* accuracy across
   the space, with a stated calibration method. (Addressed by the methodology above.)
2. **Resources harder than cycles** — lead with DSP+BRAM (near-analytical); be honest
   that LUT/FF is coarser/learned.
3. **Need a *finding*, not just a method** — the DSE must reveal a non-obvious design
   point.
4. **Need the brute-force-Vitis baseline** — the speedup + conclusion-fidelity claim, counted
   per design point, not per workload point.
5. **"Why not just have an LLM write it?"** — answer with the evaluation-cost table and, if
   step 5 is run, the agent comparison.

## Build-vs-have map (as of 2026-10-10)

| Paper piece | Status |
|---|---|
| Bit-exact functional (accuracy) | **built** — `FixedField` (`waveflow/hw/fixpoint.py`), `ComplexField` (`waveflow/hw/complexfield.py`) |
| Shared memory + queue (CG state) | **built** — `MemoryMod`, the memory-mapped views (`build_mm_device`), credit links, the crossbar |
| System verification at RTL | **built** — the system DAG (`add_system_steps`), XSI, the host-trace gate (`markov`, `mm_fir`) |
| Cycle-approximate model | **built for the existing blocks** — calibrated pysim within 2–5% of RTL on the system examples; per-block calibration of new blocks to do |
| Resource-approximate model | **built: prediction and composition** — `docs/guide/resource_model/` (per-module models, hierarchical composition, confidence, fitting from sweeps). **To do:** the active, decision-aware sampling and its validation |
| Sweeps, calibration storage | **built** — `SweepRunner` (`waveflow/build/sweep.py`), the calibration library |
| Timing / cost accounting | **built** — `waveflow.events`, `analyze_events` |
| Vector unit (CG dots / AXPY) | **new — a complete rewrite.** `examples/vecunit` is older work on the retired API (`Packet`, `HwObj`) and is not a starting point; `vecmult` (element-wise, command-response) is the nearest curated kernel to model it on |
| Systolic matmul block | **new** (application-level) |
| CG control + the CG system | **new** (application-level) |
| Incremental RTL (cheap workload points, cheap calibration) | **planned** — `plans/incremental_xsi.md` |

The infrastructure largely exists. The new work is the **application** (systolic block, vector
unit, CG control, the CG system) and the **active resource-sampling method** with its held-out
validation. The paper *composes*.

## Path to a plan

What would turn these notes into staged work, in order:

1. **The CG system in pysim, bit-exact.** Golden model, BER/MSE-vs-SNR from the bit-exact
   model alone, #iterations as a command field. This already yields the accuracy half of the
   result, with no Vitis at all.
2. **The blocks at RTL.** The vector unit, then the systolic array, each a free-running kernel;
   the system in the `bus_system` shape; the trace gate passing.
3. **Calibration** of cycles and resources per block, over each block's own parameters.
4. **The held-out validation** — the make-or-break experiment, before any DSE claim.
5. **The active sampling method**, and its comparison against random and grid sampling at
   equal synthesis budget.
6. **The DSE and the finding**; then, optionally, the agent experiment.

Prerequisites from other plans: `plans/incremental_xsi.md` (cheap RTL calibration and workload
points), `plans/maxi_pointer_fifo.md` (the CG blocks read and write shared memory through
free-running tasks, the exact structure that deadlock affects).

## Related notes
- `docs/guide/schema/python/fixpoint.md`, `complex.md` — the bit-exact fixed-point and complex
  fields (the accuracy model's foundation).
- the fft_bit_exact_notes plan ([commit 19005b5](https://github.com/sdrangan/waveflow/commit/19005b5)) — a sibling bit-exact-model idea (FFT); same harness.
- `docs/guide/resource_model/` — the resource model the active method builds on.
- `docs/guide/build/timing_events.md` — the measured evaluation costs.
- `plans/mcp_frames.md` — the blind tests, including the with/without-Waveflow comparison.
