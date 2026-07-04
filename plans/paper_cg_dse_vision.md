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

| Paper piece | Status |
|---|---|
| Bit-exact functional (accuracy) | `FixedField`/`ComplexField` — in progress; conformance harness = the "matches hardware" proof |
| Vector unit (CG dots/AXPY) | roadmap #4 (`vecunit`) |
| Shared memory + queue (CG state) | **built** — `MemComponent` + AXI-MM queue |
| Systolic matmul block | **new** (application-level) |
| CG control | **new** (application-level) |
| Cycle-approximate model | partial — timing extraction + cycle-model-training |
| Resource-approximate model | **new** — `csynthparse`/`InspectSynthStep` give actual resources; the predictive/active model is the contribution |
| DSE / build / conformance harness | **built** — `build_dag` + `run_dag_cli` + cosim rig |

Most *infrastructure* exists or is roadmapped; the new pieces are the **systolic block**,
**CG control**, and the **active resource model**. The paper *composes* — a far stronger
position than "build everything."

## Related notes
- `plans/fixedfield.md` — the bit-exact fixed-point foundation (accuracy model).
- `plans/fft_bit_exact_notes.md` — a sibling bit-exact-model idea (FFT); same harness.
- cycle-model-training (project memory) — the cycle model's calibrate-from-cosim spine.
