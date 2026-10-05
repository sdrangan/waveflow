# PySilicon DSE paper — vision notes (CG matrix inverse)

**Status: VISION NOTES, not a plan.** The north-star paper that ties the PySilicon
program together. Captured from discussion; turn into concrete plans as the pieces
land (`FixedField` → `ComplexField` → blocks → models).

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

Positioning vs prior HLS-DSE: existing work either puts the **HLS tool in the loop**
(accurate but slow — what we avoid) or uses **pure analytical models** (fast but not
functionally exact). PySilicon's angle is the **combination from one source**.

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

## Parameters & metrics

- **Parameters:** bit widths (accuracy↔DSP), memory-access width (throughput↔BRAM/
  routing), queue sizes (stall behavior), #CG iterations (accuracy↔latency), array
  size.
- **Metrics:** accuracy (BER/MSE), resources (DSP/BRAM/LUT/FF), throughput/latency.

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
burst transfer + queue stalls) and calibrated per block from cosim — the existing
**cycle-model-training** approach (fit `latency_*` params from RTL cosim). CG cycles ≈
#iters × (matmul + vector + memory + queue-stall) per-block cycles. Same per-block,
calibrate-from-runs structure as the resource model.

## Experimental structure

1. **Calibrate** — per-block syntheses/cosims to fit the resource + cycle models.
2. **Validate** — held-out design points: show predicted vs actual cycles/resources are
   accurate *across the space*, not just at calibration points. (This is the make-or-
   break rigor.)
3. **DSE** — sweep the full parameter cross-product in Python (exact accuracy +
   predicted performance); produce the accuracy/resource/throughput Pareto frontier.
4. **Baseline + finding** — (a) quantify the win: brute-force Vitis at every point =
   X compute-days vs PySilicon = Y minutes + K calibration runs, conclusions matching
   ground truth on the validation subset; (b) a concrete **design finding** (e.g.
   "12-bit + 8 iterations hits target BER at half the DSPs of naive 16-bit/12-iter").

## Reviewer risks / make-or-break

1. **Approximate-model validation** is the whole ballgame — *held-out* accuracy across
   the space, with a stated calibration method. (Addressed by the methodology above.)
2. **Resources harder than cycles** — lead with DSP+BRAM (near-analytical); be honest
   that LUT/FF is coarser/learned.
3. **Need a *finding*, not just a method** — the DSE must reveal a non-obvious design
   point.
4. **Need the brute-force-Vitis baseline** — the speedup + conclusion-fidelity claim.

## Build-vs-have map

Refreshed 2026-10-05. The executable plan for this paper's simulations is
[`plans/mimo_cg/mimo_cg_paper_sims.md`](mimo_cg/mimo_cg_paper_sims.md): massive-MIMO uplink CG
detection on `xczu48dr` with Vitis 2024.1. **All of its phases have run** (Phase 6 awaits its
review); everything application-level lives in `examples/mimo_cg/`, and the framework gained no
CG-specific code.

| Paper piece | Status |
|---|---|
| Bit-exact functional (accuracy) | **built and used** — `FixedField` and `ComplexField`, plus the example's fixed-point CG golden (`examples/mimo_cg/mimo_cg_fixed.py`) with the division α and β need. Accuracy was swept over 27 scenarios, 7 widths, 3 guards and up to 8 iteration counts with no Vitis (plan Phases 2–3) |
| Vector unit (CG dots/AXPY) | **built** — `examples/mimo_cg/hw/vec.py` and `cpp/cg_vec_task.h`: lanes as a knob, per-column α and β with one divider per lane |
| Shared memory + queue (CG state) | **built** — `MemoryMod` and the framework's memory streams; the blocks exchange P and S through stream-of-blocks channels |
| Systolic matmul block | **built** — `examples/mimo_cg/hw/mm.py` and `cpp/cg_mm_task.h`: an R × C array with a 3- or 4-multiply complex multiplier |
| CG control | **built** — the free-running composite `CgDetector` (`examples/mimo_cg/hw/detector.py`), bit-exact at RTL for K = 4, 8, 16 |
| Cycle-approximate model | **built for this design on xczu48dr** — `examples/mimo_cg/hw/models.py`: counted trip counts plus a fitted per-tile overhead; 1.0% mean job-time error over 8,807 measured jobs |
| Resource-approximate model | **built for this design on xczu48dr** — the same file: DSP and block RAM counted (exact on 1,440 of 1,440 measured detectors), LUT and FF fitted per block (0.9% and 2.3% mean error). The framework's `compose` walks the same numbers |
| DSE / build / conformance harness | **built and used** — `SweepRunner` drives the campaigns; `examples/mimo_cg/hw/dse.py` prices 6,084,720 joint designs in 15 s |
| Brute-force baseline | **run** on a slice — 1,440 detectors (1.3% of the configurations), 57 tool-hours; the models' pick is within 10% of the best in 99.8% of 2,592 pre-registered decisions (`examples/mimo_cg/hw/fidelity.py`) |
| Sampling experiment | **run as a learning curve** on existing data: with 45 of the 86 calibration builds, 19 of 20 random subsets pass the same 90% bar. Uncertainty- or decision-aware sampling is not built |

**Where the experimental structure stands.** (1) Calibrate: 86 builds, 2.1 tool-hours. (2) Validate:
46 held-out builds, each set fixed before the calibration round it tests (34 before the first, 12
after it; the second models were refitted after 40 of them had been scored), then the 1,440-build
sub-grid. (3) DSE: the whole
cross-product in Python. (4) Baseline and finding: the brute force of the sub-grid took 57
tool-hours and the whole space is projected at about 4,000; the design finding is what guard bits
on two scalars are worth (a median 8% of LUTs and 15% of flip-flops by the models' csynth numbers,
2–18% and 3–18% as implemented on six pairs, and feasibility itself for a quarter of the
questions). The numbers and their caveats are in the plan's sections 14 to 16.

**Reviewer risks, revisited.** The approximate models were validated on builds they never saw, and
the weak spots are stated (one mispredicted family; csynth is not the implemented design; timing
closure is shown up to 110k csynth LUTs only; the brute force is a slice). Resources
did lead with DSP and block RAM, which are exact. The finding is modest in size and stated as such.
The brute-force baseline exists.

## Related notes
- `plans/fixedfield.md` — the bit-exact fixed-point foundation (accuracy model).
- `plans/fft_bit_exact_notes.md` — a sibling bit-exact-model idea (FFT); same harness.
- cycle-model-training (project memory) — the cycle model's calibrate-from-cosim spine.
