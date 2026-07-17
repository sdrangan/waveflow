# Integrating AMD Vitis BLAS (GEMV / GEMM) as Waveflow HwComponents

**Status: PLAN, not yet started.** Roadmap item #5 ("Vitis L1 wrapper"), the concrete
first delivery of the promise in `docs/overview/status.md:36` ("modules wrapping GEMM
and FFT blocks for Vitis L1 DSP").

Goal: a Waveflow `HwComponent` whose C++ body **calls AMD's BLAS HLS code** rather
than reimplementing it, and whose Python side is a **golden model** we validate two
ways — functionally (Part A: bits) and temporally (Part B: cycles).

## Premise

We do not rewrite GEMM/GEMV. AMD ships them as HLS C++ templates. Waveflow's job is:

1. **Generate the shell** — top signature, `m_axi` bundles, `s_axilite`, command
   decode, operand load/store. This already works and is not new work.
2. **Call the vendor function from a hook** — a hand-written adapter body.
3. **Model it in Python** — bit-exact where achievable, cycle-accurate by calibration.

The seam already exists and is exercised three times in the histogram; see
[Mechanism](#mechanism-this-is-already-built) below. The genuinely new work is Parts A
and B plus a small amount of build plumbing.

## Relation to `fft_bit_exact_notes.md`

That note draws a sharp line and it applies here:

- **Wrap the IP** (this plan): call vendor code; treat it as a black box.
- **Replicate the IP** (that note): reimplement bit-exactly in Python; no Vitis needed.

The note assumes wrapping implies a *float golden + SNR* gate, i.e. **not** bit-exact.
**For GEMV/GEMM in fixed point that assumption is wrong, and this plan exploits why**
— see [A.0](#a0-why-bit-exact-is-achievable-here-and-when-it-isnt). Bit-exactness
turns out to be *easier* for BLAS than for FFT, which is a good reason to do BLAS
first: it is the same conformance rig on a much shorter algorithm.

---

## Stage 0 — Reconnaissance (BLOCKING; do this before anything else)

Two facts are unverified and **either could reshape the whole plan**. Do not skip.

### 0.1 Does L1 GEMM exist, and is it stream-based?

The working assumption is that L1 BLAS primitives are **stream-in/stream-out template
functions** over `hls::stream<WideType<T, ParEntries>>` that deliberately carry no
`m_axi` (that's what L2 kernels add). If true, this is an *excellent* fit: our
generated shell already owns the memory side (`MemRStream`/`MemWStream`,
`read_array_slice`), and the hook is a thin stream adapter — the load-compute-store
anatomy already validated on the interleaver, with the vendor block as the compute
stage.

**The risk:** BLAS levels are a naming collision. Netlib BLAS "level 1/2/3" =
vector-vector / matrix-vector / matrix-matrix, and the Vitis directory names `L1/L2/L3`
mean something else entirely (primitive / kernel / host). It is entirely possible that
**GEMM (matrix-matrix) exists only as an L2 *kernel* that owns its own `m_axi` and
memory layout**, not as an L1 primitive. If so, GEMM is not a hook at all — it's a
whole vendor kernel to integrate, which is a *different architecture* (it owns memory;
we would be composing with it, not calling it). GEMV is far more likely to be a true
L1 primitive.

**Therefore: GEMV is the first target, not GEMM.** It proves the entire seam at lower
risk. GEMM follows once 0.1 is answered, and may need a separate plan if it turns out
to be L2-only.

Deliverable: a short written answer — for each of GEMV and GEMM — giving the header
path, the exact template signature, the datatypes supported, and whether it carries
`m_axi`. Record it in this file before writing code.

### 0.2 Acquiring the library

**Verified: Vitis 2025.1 here does not ship Vitis_Libraries.** `C:\Xilinx\2025.1`
contains `Vitis/ Vivado/ Model_Composer/ data/ gnu/ tps/ win64/` and no `*librar*`
directory. So the library must be obtained separately (GitHub `Xilinx/Vitis_Libraries`).

Decide and record: **submodule vs. detected-on-disk**. Recommendation: **detect**,
mirroring the existing toolchain pattern at `waveflow/toolchain/toolchain.py:123-142`
(`WAVEFLOW_VITIS_PATH` env override, then a list of candidate roots). Add
`find_vitis_libs_path()` with `WAVEFLOW_VITIS_LIBS_PATH`. Rationale: the repo already
treats Vitis as optional-and-detected; vendoring a large third-party tree cuts against
that and drags in licensing questions. Tests gate on presence, exactly like `-m vitis`.

### 0.3 Accumulator behavior (feeds Part A)

From the vendor source, determine for the chosen datatype: the **accumulator width**,
and whether **any rounding or saturation happens mid-accumulation**. This single fact
decides whether Part A's bit-exact gate is cheap or expensive. See A.0.

---

## Scope: the template subset

We implement a deliberately small subset and **fail loud** outside it — the same v1
posture as `FixedField` (which rejects >64-bit and mixed-sign rather than guessing).

| Axis | v1 | Deferred |
|---|---|---|
| Datatype | one fixed-point type (e.g. `ap_fixed<16,2>`) | float, double, multiple widths |
| `ParEntries` | one value, pinned at elaboration via `HwParam` | sweep as a design-space axis |
| Op | GEMV first, then GEMM | symv, trmv, gbmv, the rest of BLAS |
| Transpose | none (row-major, no-transpose only) | trans variants |
| alpha / beta | pinned `alpha=1, beta=0` | general scaling |
| Strides | `incx == incy == 1` | strided vectors |
| Dims | multiples of `ParEntries` | ragged tails / boundary peel |

Anything else raises at elaboration with a message naming the unsupported axis. The
subset is a **contract**, not a TODO list: a narrow, verified block beats a broad,
plausible one.

---

## Mechanism (this is already built)

For reference while implementing — no new framework work here:

- `@synthesizable` with **no** `synth_fn` = "codegen emits a call to a C++ function I
  write by hand" (`waveflow/hw/synth.py:15-51`). The extractor turns it into a
  `FunctionStmt` (`waveflow/build/hwcodegen.py:1594-1607`).
- The impl file is **sticky**: the generator writes a `TODO` stub once and never
  touches it again (`waveflow/build/hwcodegen_steps.py:150-155`). A hand-written
  vendor adapter survives every regeneration.
- `.cpp` vs `.tpp` is automatic, not declared — a hook with an `HwParamValue`-width
  stream arg becomes templated `.tpp` and is `#include`d from the generated header;
  otherwise it's a plain `.cpp` translation unit that must be `add_files`'d
  (`waveflow/build/hwgen.py:1024-1046`).
- Copy the histogram: `examples/shared_mem/hist.py:394-437` (three hooks),
  `cpp_namespace` at `hist.py:335`, and `run.tcl:36-41` for the `add_files` lines.
- Codegen still emits everything else — signature, `m_axi ... offset=slave bundle=gmem`,
  `s_axilite`, and the qualified call site (`hwgen.py:736`, `hwgen.py:1487`).

**Component surface** (following VMAC, `examples/vmac/vmac.py`):

- `run_proc` / `on_start` — kernel shell. *Not* the model, and it does not call the
  vendor function directly.
- `gemv_compute(...)` — the `@synthesizable` hook. **Its Python body is the sim model**;
  its C++ body is the vendor call. Two implementations of one contract.
- `execute(...)` — the pure, memory-free golden (VMAC's naming, `vmac.py:222`).

Note the correction to the original framing: the hook is a *method beside* `run_proc`,
not `run_proc` itself.

---

## Part A — Functional validation (bit-exact)

### A.0 Why bit-exact is achievable here (and when it isn't)

This is the load-bearing argument of Part A.

**Fixed-point MAC into a full-width accumulator is order-independent.** Products of
fixed-point operands are *exact* integers; accumulating them is *integer* addition,
which is associative and commutative. So the vendor's reduction tree, systolic
schedule, and `ParEntries` **cannot change the result bits**. Bit-exactness collapses
to matching four things:

1. input quantization,
2. product width,
3. any intermediate rounding/saturation (**must be none** — this is Stage 0.3),
4. the final requantization of accumulator → output type.

All four are static properties readable from the vendor source. This is dramatically
easier than the FFT case, where twiddle quantization and per-stage scaling are the
risk corners.

**Float is a different story and we should not pretend otherwise.** Float addition is
not associative, so the accumulation order *does* change the low bits; numpy
accumulates in OpenBLAS's order and the vendor accumulates in its systolic order, and
they will disagree. Bit-exact float would require the Python golden to replicate the
hardware reduction tree — at which point the "golden" is a hardware model, not a
reference. **v1 is fixed-point only**, precisely so that "bit-exact" means what it
says. If float is ever needed, it gets a tolerance/SNR gate (which is what
`fft_bit_exact_notes.md` assumed for wrappers) and that must be stated plainly rather
than blurred into the same word.

**If Stage 0.3 finds mid-accumulation saturation**, order-independence is void and A.0
must be revisited before proceeding — likely by pinning dims small enough that
saturation provably cannot occur, and asserting that bound at elaboration.

### A.1 Golden

Pure numpy `execute(...)` over `FixedField`-typed operands. Reuses the existing
fixed-point conformance harness (34/34 Vitis bit-exact precedent) — do not build a new
rig.

### A.2 Gates

- **Gate A1 — csim bit-exact.** Generated top + vendor hook, csim, byte-identical vs.
  the Python golden. Random operands + edge cases (zeros, saturation-adjacent values,
  min/max of the fixed type).
- **Gate A2 — csynth clean.** RTL exists, top has real outputs, no dangling `m_axi`.
- **Gate A3 — cosim bit-exact.** The RTL agrees with the golden, not just the C model.

**A2 is not a formality.** Two standing traps make csim pass while RTL is broken
(`reference-hls-hook-csynth-gotchas`): nested-struct-by-value DCEs the kernel (pass
scalars to a `*_core` function), and a non-inlined datapath leaves the top with "no
outputs" (needs `#pragma HLS INLINE`). Both are invisible at csim. Check the report,
don't trust the exit code.

---

## Part B — Timing validation (build a timing model)

### B.0 The key asymmetry vs. our own kernels

For kernels we generate, we know `II=1` **by construction** and the LT model is nearly
derivable. **The vendor block is a black box.** We do not control its internal
schedule and should not guess it. So the compute model is **fit from a cosim sweep**,
then validated on held-out points — not derived from first principles.

This is exactly the shape of the VMAC Stage 5 II-decoupling work (calibrated from a
cosim sweep, held-out error 1.54%) and the FIR block-fidelity work (gates 0.11 / 0.14 /
0.60%). Reuse `waveflow/calib/` — `LinCalibModel` / `InterpCalibModel`, the uniform
`state_dict` + artifact-file + seed pattern. **Do not hand-roll a fitter.**

### B.1 Two-level structure (non-negotiable)

Per `project-two-level-calibration`: bus transfer is a **platform property**,
characterized once (`loadstore_iso`), *not* per-accelerator. Only the **kernel compute**
is fit per-block. The payoff is the falsifiable claim:

> **Gate B3 (zero-fit):** with the platform bus model and the compute model each
> already fit, the *loaded end-to-end* prediction must emerge with **no additional
> fitting**. If it needs a fudge factor, the decomposition is wrong.

### B.2 Model form

Fit compute cycles as a function of the problem dims — `(m, n)` for GEMV, `(m, n, k)`
for GEMM — at the pinned `ParEntries`. Expect roughly affine in the dominant dim with a
fixed pipeline-fill term; let the sweep decide rather than assuming.

**Measure occupancy, not span** (`project-fir-stageb-occupancy-model`). And heed the
FIR root-cause: a hook using `read/write_array_slice` builds a 2-pass resident buffer
and **loses ~2× duplex** vs. a canonical lane loop (direct 306 vs. buffered 534). If
the GEMV adapter buffers operands before handing them to the vendor stream, the timing
model will faithfully fit a **self-inflicted** inefficiency. Prefer streaming the
operands straight in; if buffering is unavoidable (GEMM likely needs a resident tile),
say so explicitly rather than discovering it in the residuals.

### B.3 Gates

- **Gate B1 — sweep fit.** Compute model fit over a dim sweep; report coefficients.
- **Gate B2 — held-out.** Error on held-out points **< 2%** (precedent: VMAC 1.54%).
- **Gate B3 — zero-fit end-to-end.** As B.1. This is the real test.

### B.4 Cosim is the gate, not XSI

`reference-hls-task-no-maxi` is a verified 2025.1 fact: `hls::task` / `ap_ctrl_none`
**cannot carry `m_axi` or `s_axilite`**. A BLAS block with memory-mapped operands is
therefore a **DATAFLOW** synth target, and its verification scope is **Vitis unit
cosim**. The XSI BFM harness does not apply — it is for free-running `ap_ctrl_none`
tiles. Do not plan XSI cycle gates here.

---

## Plumbing gaps (must fix; small but shared code)

1. **`render_tcl` cannot add include paths.** `waveflow/build/composite_gen.py:768-790`
   hardcodes `set cf "-I{INCLUDE_DIR}"`; `extra_sources` only appends `add_files`
   lines. Reaching `blas/L1/include/hw/xf_blas/` needs an `extra_includes` parameter.
   This touches shared build code used by every example — change it additively, with a
   default that leaves existing output byte-identical.
2. **Library location must reach the tcl.** Wire `find_vitis_libs_path()` (0.2) through
   to that new parameter. Skip loudly when absent, like the existing Vitis gates —
   a soft-skip that silently passes is worse than a failure
   (`reference-vitis-installed-here`).
3. **`impl_file` is a bare filename** resolved relative to the example root, so it
   cannot point out-of-tree. **This is fine and needs no change** — the in-tree hook is
   a thin adapter that `#include`s the vendor header. Noted so nobody "fixes" it.

---

## Sequencing

| Stage | Deliverable | Gate |
|---|---|---|
| 0 | API recon + library acquisition + accumulator facts, recorded in this file | 0.1 answered in writing |
| 1 | `render_tcl(extra_includes=...)` + `find_vitis_libs_path()` | existing examples byte-identical |
| 2 | GEMV component: shell + hook + golden; csim | **A1** |
| 3 | GEMV csynth + cosim | **A2, A3** |
| 4 | GEMV timing: sweep, fit, held-out, zero-fit | **B1, B2, B3** |
| 5 | GEMM — **scope only after 0.1**; may need its own plan if L2-only | A + B repeated |

Stages 2–4 on GEMV constitute the real proof. GEMM inherits the seam.

## Risks

- **GEMM may not be an L1 primitive** (0.1). Biggest structural risk; mitigated by
  doing GEMV first.
- **Mid-accumulation saturation** would void A.0's order-independence argument (0.3).
- **Float pressure.** If a consumer wants float GEMM, "bit-exact" is off the table —
  that is a property of float arithmetic, not a gap in our rig. Re-gate on SNR and say
  so out loud.
- **Vendor version drift.** The fit in Part B is calibrated against one library
  version. Record the version in the calib artifact; a library bump invalidates the
  timing model, not just the build.

## Sources

- [Vitis BLAS library docs](https://xilinx.github.io/Vitis_Libraries/blas/)
- [Vitis_Libraries source](https://github.com/Xilinx/Vitis_Libraries)
- Sibling note: `plans/fft_bit_exact_notes.md` (wrap-vs-replicate distinction)
- Anatomy precedent: `examples/shared_mem/hist.py`, `examples/vmac/vmac.py`
</content>
</invoke>
