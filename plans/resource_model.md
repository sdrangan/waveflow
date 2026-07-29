# Plan: the resource model — per-module FPGA resource prediction for DSE

> **Status (2026-07-28): Phases A–E COMPLETE and gated. The arc is closed; docs for D/E are the
> remaining work.**
>
> * A1 — `waveflow/calib/module_key.py` + `tests/calib/test_module_key.py` (18 tests).
> * A2 — `waveflow/calib/record_store.py` + `tests/calib/test_record_store.py` (25 tests), plus the
>   `modules/` tier wired into `publish.py` with its own coverage guard (4 tests).
> * A3 — `waveflow/calib/confidence.py` + `tests/calib/test_confidence.py` (26 tests), the
>   `CalibModel`/`StreamTimingModel` hooks, and `BuildResult.elapsed_seconds`.
> * B1 — `waveflow/calib/synth_report.py` + `waveflow/build/resource_steps.py` +
>   `tests/calib/test_synth_report.py` (18 tests). Attribution gated against the **real** committed
>   `fir_block` report, so the arithmetic asserted is the arithmetic that actually held. See
>   [the first measurement](#the-first-measurement-and-what-it-already-says) — DSP is already
>   perfectly additive.
> * B2 — the 24-point sweep, **24/24 in 20.5 min**. Three results worth reading before D1:
>   the [integration term is exactly constant](#the-integration-term-is-exactly-constant), the
>   [DSP prior is a two-sided step function](#dsp-is-a-two-sided-step-function), and the
>   [reuse claim is now measured](#the-reuse-claim-measured) (the mem-streams were characterized once
>   and served all 24 points).
>
> Docs: `guide/calib` retitled **Model calibration** and extended with `modules.md` + `resources.md`;
> the section now covers both quantities, since only the *source* of a number differs.
>
> * D1 — `examples/fir_block/fir_block_resource.py` + `waveflow/calib/resource_model.py`.
>   **24/24 exact on DSP and BRAM with zero fitted parameters**, and the `mem_dwidth` test confirms
>   E1's third term is [boundary-only](#the-mem_dwidth-test--the-interface-term-is-boundary-only).
> * D2 — `FittedResourceModel` for LUT/FF, held out at 7.1%/9.8% mean, plus
>   `examples/fir_block/fir_block_corpus.py`, the 24 measured points as committed source.
> * E1/E2 — `InterfaceResourceModel` + `compose()`, validated against design totals that fit nothing:
>   **DSP and BRAM exact at 24/24**, LUT 3.2% / FF 2.8% mean, rank correlation 0.95–1.00. See
>   [the honesty notes](#e2--held-out-validation-) before quoting those figures.
>
> Suite at its 6-failure baseline throughout; no regressions.
>
> **Open items.** C1 is deprioritized — B2 showed in-composite attribution already yields per-module
> numbers, so a standalone harness is now only needed to characterize a module *before* any composite
> exists and to test the additivity assumption. It still carries an unresolved design question: see
> [Open decision: boundary-port depth](#open-decision--boundary-port-depth-a-c1-blocker). The 30
> measured module configurations sit in the untracked work tier and have **not** been published to the
> shared platform library.
>
> The pilot is [`examples/fir_block`](../examples/fir_block), which is green through pysim → csynth →
> XSI and carries exactly the knobs the methodology needs to be tested against.

## Motivation

[`paper_cg_dse_vision.md`](paper_cg_dse_vision.md) commits to a **cycle- and resource-approximate**
model calibrated from a handful of Vitis runs, so that DSE over a large parameter cross-product runs
in Python. The cycle half has a spine already (`ComponentFixture` → `StreamTimingModel` → the platform
library, closed to 0.0% on `mem_copy`). **The resource half does not exist**, and it is the piece the
paper's headline claim rests on.

This plan builds it. The target consumer is not `fir_block` — it is the CG matrix inverse, where `N`
modules are instantiated from a subset of `P` system parameters and the DSE agent must choose `p`
under a resource constraint. Everything here is therefore shaped by one question: **what must be true
of the store now so that an agent can later ask "what does this design cost, and do you actually
know?"**

### The framing that makes this tractable

Three commitments, taken from the vision notes and sharpened in discussion:

1. **Per-module models turn a combinatorial space into an additive one.** Model each module from *its
   own* few parameters, synthesize modules independently, and predict the cross-product as a sum. You
   synthesize O(Σ per-module ranges), never the product.
2. **Don't learn known physics.** DSP ≈ multiplier count × DSP-packing(width) and BRAM ≈
   `ceil(depth·width / block)` are *known step functions*. Encode them; learn the smooth residual on
   top. This also fixes a real failure mode — smooth regressors predict block-granularity jumps badly.
3. **A point estimate is not an answer.** Every prediction carries whether it was interpolated inside
   the calibrated hull or extrapolated outside it. An agent optimizing against a bare float walks out
   of the calibrated region and confidently reports a design that does not exist.

## What already exists

| Piece | Where | State |
|---|---|---|
| Structural identity of a `(class, params)` | [`elaborate.structure_signature`](../waveflow/build/elaborate.py) | **built** — name/identity-agnostic canonical signature |
| Per-platform calibration library layout | [`calib/platform.py`](../waveflow/calib/platform.py) | **built** — `platform.json` pins `(part, clk)`; `components/<id>/` holds fits |
| Per-component harness contract | [`calib/fixture.py`](../waveflow/calib/fixture.py) | **built, timing-only** — `sweep()` / `run_pysim()` / `rtl_firings()` + a registry |
| Model fit/predict/artifact | [`calib/calib.py`](../waveflow/calib/calib.py) | **built** — `state_dict` params + JSON artifact; `predict -> float` |
| csynth report parsing | [`utils/csynthparse.py`](../waveflow/utils/csynthparse.py) | **built** — totals, **per-RTL-module breakdown**, loop/II info |
| Two-tier calib storage | `calib/work/` (untracked) vs `calib/platforms/` (tracked) | **built** — `publish_calib` promotes |
| The pilot design | [`examples/fir_block`](../examples/fir_block) | **built** — pysim + csynth + XSI green, 5 knobs |
| Resource **store** and **model** | — | **new; this plan** |

The genuinely new engineering is the store, the standalone-module synthesis path, and the priors. The
rest composes.

## Why `fir_block` is the pilot

Not arbitrary — its knobs exercise every claim above, at a size that fits in one overnight run.

* **DSP is a coupled step function, not a trivial one.** `unroll_lane=False` → `NTAP` multipliers;
  `unroll_lane=True` → `NTAP × LW`, where `LW = mem_dwidth // samp_w` **moves with the width knob
  itself**. So the prior composes two step functions, `LW(samp_w) × dsp_per_mult(samp_w)` — and
  [they cancel](#the-unrolled-plateau-is-two-effects-cancelling), which is exactly the kind of result
  a prior earns and a fit would only mimic. (The `add_state` plan already anticipated the DSP48E1
  25×18 packing cliff; this is where it got measured.)
* **BRAM has a discontinuity we ourselves emit.** The tap/history storage carries an
  `ARRAY_PARTITION` pragma from `add_state`, and partitioning *converts BRAM into LUT/FF* past a
  threshold. The exact "smooth models miss the jump" failure mode, in the smallest example we have.
* **`unroll_lane` is a structural knob, not a feature.** It is a different circuit, so it must select
  a *different model* rather than become a regression column. Signature keying does this by
  construction — and that is the cheapest available proof that the keying decision is right.
* **It is affordable.** 3 `ntap` × 4 `samp_w` × 2 realizations = **24 whole-top csynths in 20.5
  minutes** — far cheaper than the overnight run this section originally budgeted for, which is what
  made a second `mem_dwidth` probe a casual decision rather than a commitment.

## Design decisions

### 1. The key is the structure signature, not a param tuple

`module_key(cls, params)` = a short human prefix plus a hash of
[`structure_signature`](../waveflow/build/elaborate.py) — `fir_compute-a3f19c`. The prefix is for
grep-ability; the hash is for correctness.

This is what makes "each module is instantiated by some subset of the P system parameters"
**mechanical rather than hand-declared**: elaborate the system top, walk `sub_comps`, and each leaf's
resolved signature *is* its cache key. Two different system-level `p` that induce the same module
config hit the same cached synthesis — which is most of the DSE saving, for free.

Corollary: keep **compile-time params** (what selects the model) distinct from **workload features**
(what the model is evaluated at). Resources depend only on the former. Timing depends on both, which
is why the existing `basis` (`nwords`, `num_trans`) lives on the timing side only.

### 2. One record envelope for both axes

```
{key, target, source, cost_seconds, payload}
```

* timing → `source: pysim | cosim | xsi`, payload `{features{...}, cycles}`
* resource → `source: hls_estimate | vivado_synth | vivado_impl`, payload
  `{lut, ff, dsp, bram, uram, srl, achieved_period_ns}`

`source` and `cost_seconds` are not bookkeeping. `source` is what lets HLS estimates be upgraded to
post-implementation numbers later as a *data addition* rather than a schema migration.
`cost_seconds` is what the agent budgets against, and what makes the paper's "K ≪ N syntheses" claim
auditable from the library itself instead of from a lab notebook.

### 3. Resources live under the platform, keyed by `(part, period)`

LUT/DSP/BRAM counts are meaningless without the part family, and HLS schedules to the target period,
so both belong in the key. They already are: `platform.json` pins them and `Platform.resolve` is the
create-or-confirm gate. Reuse it rather than inventing a parallel notion of target.

### 4. Prior and residual are stored separately

The artifact is `{prior_spec, residual_params}`, never collapsed into a single fitted number. A prior
that needs no fitting must be *visible* as such — "DSP predicted exactly, zero fitted parameters" is a
result worth being able to state, and it is unstatable if the prior is baked into a regression
intercept.

### 5. Three additive terms, not one

Whole-design estimate = Σ per-module + Σ per-interface + fixed shell. Sub-modules of a free-running
composite are separate C++ tasks and roughly additive, but the `m_axi` adapters and interconnect are
**shared** and scale with port count and width. Since endpoint direction is a first-class type now,
the interface term is computable from the boundary structure rather than fit blindly.

The handful of whole-top syntheses is **not optional** — it is what turns additivity from an
assumption into a measured claim.

### 6. Confidence is a level plus free-form facts — **not** an interval

The obvious move, attaching `lo`/`hi`, is wrong here *in principle*. Synthesis is deterministic — run
csynth twice at a point and the LUT count is identical — so there is no noise process for a prediction
interval to estimate. The error that actually occurs is **model misspecification**, which is not
measurable from inside the model, and which is systematic exactly where it hurts: near a partitioning
threshold a smooth model's residual is consistently wrong in one direction, and a Gaussian interval
would understate it.

Cross-validated spread is wrong for a second reason: an affine span law fit at n=128 and n=256
predicts every other n *exactly*. LOO-CV would manufacture a ±3% band where the true error is zero.
Only the model knows its own epistemic situation, so the model speaks.

So: **`Confidence(level, facts)`**, where `level` is a **closed, sortable** four-value enum
(`EXACT` → `INTERPOLATED` → `EXTRAPOLATED` → `UNCALIBRATED`) and `facts` is a free-form JSON-able dict
in whatever vocabulary that model uses. The enum stays closed because its only job is triage across N
modules; the moment it grows, the ordering becomes ambiguous and triage — its whole purpose — breaks.
The test for a new level is whether a consumer would take a *different action*, and there are only
three actions: trust it, spend a calibration, avoid the region. Everything finer is explanation.

Two guards keep the freedom from becoming a junk drawer: facts are checked JSON-serializable at
construction (so a stray `numpy.float64` fails at the model, not at report-dump time), and an `EXACT`
claim must be **backed by zero residual on the model's own corpus**.

**`predict` stays a bare float.** It sits on the simulation hot path — `hw_freerun`, `memif`, and the
example kernels call it per firing and do arithmetic on the result. `estimate(row) -> Estimate(value,
source, confidence)` is the reporting-time entry, built once per module.

### 6b. Two rules that fell out of building it

* **An exactness claim needs more points than free parameters.** An affine form has two; fitting it at
  exactly two points gives zero residual *by construction*, which is evidence of nothing. Only an
  over-determined fit says something about the form — that the law kept holding at points it was not
  free to match. `FitSummary.degenerate` marks the other case.
* **Leaving the sampled region outranks a confirmed form.** A verified law is verified only *where it
  was measured*; what breaks it outside is a regime change (a burst-splitting limit; a DSP-vs-LUT
  inference threshold), not fit error. So the level drops to `EXTRAPOLATED` — but `form_exact` rides
  in the facts, because extrapolating a law that held at every point is a far better position than
  extrapolating a noisy one, and flattening that away would lose the distinction that matters.

### 6c. Cost is recorded, never modelled

`BuildResult.elapsed_seconds` is measured for every step, success or failure. That raw history is what
later answers "what would recalibrating here cost?" — better than any estimate, and unrecoverable if
never measured. The query surface (median per module key, local history preferred, since **cost is
machine-local while the measurement is not**) is deferred until there is history worth querying.

### 7. Per-module numbers come from the report *before* they come from a standalone synthesis

`CsynthParser.get_module_resources()` already returns a **per-RTL-module breakdown from the whole-top
report**. That is a nearly-free first cut at per-module attribution, and it de-risks the expensive
part of this plan: we learn whether per-module numbers sum sensibly *before* building the standalone
synthesis harness. The catch is name resolution — HLS RTL module names are mangled and must be mapped
back to Waveflow modules, which is a real (small) piece of work and a documented trap below.

Standalone synthesis still gets built (Phase C), because the report breakdown cannot tell you what a
module costs *outside* this composite — which is the whole point of a reusable library.

## Open decision — boundary-port depth (a C1 blocker)

Found while building A1, and it decides whether Phase C's data can join Phase B's at all.

A stream endpoint's `queue_size` is `None` until `Interface.bind` supplies the channel depth. Since
**FIFO depth is physical** — it costs resources and shapes backpressure — depth belongs in the key,
and an *unbound internal* port means the structure is simply not determined yet. A1 enforces that
(`UnboundModuleError`), which also aligns the key with two invariants that already exist: codegen
refuses `depth=None`, and an unbound endpoint simulates with unbounded capacity (the condition under
which backpressure silently disappears).

But a **boundary** port faces outside the design, so its depth is the *enclosing context's* to set.
A1 exempts those from the boundness check, using the derived `boundary` list. That leaves a question
the digest still answers implicitly:

> The same module keys differently standalone vs. inside a composite, because in one case its ports
> carry the harness's depths and in the other the composite's.

Two consequences, one wanted and one not:

* **Wanted** — if the C1 harness binds at *different* depths than the composite uses, that genuinely
  is different hardware and the records genuinely should not join. The key surfacing this is a
  feature. C1's harness must therefore bind at the composite's depths, deliberately.
* **Unwanted** — a *boundary* port's depth is not the module's property in either setting, yet it
  still lands in the digest. So a module whose boundary depth differs between harness and composite
  gets two keys for one circuit.

**The decision C1 must make:** either mask boundary-port depth out of the digest (a signature variant
that nulls exempted endpoints before canonicalizing), or require harnesses to reproduce boundary
depths exactly. Masking is more principled; requiring is simpler and needs no new machinery. Do not
resolve this speculatively — resolve it against the first real standalone synthesis, where the
resource delta between the two bindings is measurable rather than argued.

## Phase A — foundation (no toolchain)

### A1 — the module key ✅
`waveflow/calib/module_key.py`: `module_key(cls, params) -> str` and a `module.json` identity record
(class, resolved params, signature hash, source rev).

**Gate (passed):** two system-level param sets inducing the same `FirCompute` produce the same key;
flipping `unroll_lane` produces a different one; the key is stable across a subprocess with a
different `PYTHONHASHSEED`; an address leak and an unbound internal port each raise rather than
yielding an unhittable key.

### A2 — the record store ✅
`waveflow/calib/record_store.py`: the `{key, target, source, cost_seconds, payload, provenance}`
envelope, `modules/<key>/{timing,resource}/records.jsonl` under the existing platform layout, and the
`modules/` tier added to `publish.py` so it rides the established two-tier promotion (with a
record-count regression guard rather than a second policy).

`provenance` turned out to be the load-bearing field rather than a bookkeeping one: it pins the full
structure digest, part, period, and tool, so `read()` **verifies** a record against the module being
asked about instead of trusting the directory name. JSONL rather than CSV because payloads are nested
dicts that differ by axis and appends must never rewrite existing rows.

**Gate (passed):** records round-trip and append; a record whose provenance digest does not match the
identity raises `StaleRecordError`; two identities under one key raise `KeyCollisionError` rather than
merging; `~0` normalizes to `0` at the boundary; `best()` prefers the strongest fidelity tier; the
`fir_block` walk files one record per keyed module and the mem-streams are shared across a width
sweep.

### A3 — `Confidence` ✅
`waveflow/calib/confidence.py`: `ConfidenceLevel`, `Confidence`, `Estimate`, `FitSummary`.
`CalibModel` gains `confidence()` / `estimate()` with the level **derived** from the retained fit
summary, plus a `_confidence_facts()` hook per subclass; `StreamTimingModel` gets the passthrough a
report needs. `BuildResult.elapsed_seconds` is now recorded for every step.

`FitSummary` is what makes this work for a *deployed* model: a fitted model discards its training data
(that is what lets a published artifact predict with no sklearn and no corpus), so the support region
and worst residual are retained explicitly and ride in the artifact under a reserved key — additive,
so older artifacts load unchanged.

**Gate (passed):** suite at its 6-failure baseline, `mem_copy` calibration included. An
under-determined fit is refused `EXACT`; an out-of-range query cannot report `EXACT`; a seed fallback
and a summary-less artifact both report `UNCALIBRATED` and are distinguishable in the facts;
non-serializable facts raise at construction; `predict` is still a bare float.

**Found while building:** the two packaged `zynq7020_bfm_100mhz` component artifacts predate fit
summaries, so they predict real fitted values (13.50 / 23.00 cycles) but correctly report
`UNCALIBRATED` with `has_fitted_params: True`. A re-run of the platform sweep would regenerate them
with support recorded — worth doing when the toolchain is next in use, not urgent.

## Phase B — first real data (existing toolchain path)

### B1 — `InspectSynthStep` ✅
`waveflow/calib/synth_report.py` (attribution) + `waveflow/build/resource_steps.py` (the step) +
`tests/calib/test_synth_report.py` (18 tests), wired into the `fir_block` DAG as step `resources`.
`CSynthStep` now publishes `synth_seconds` as an artifact so the record's `cost_seconds` is captured
at the moment it is spent.

**The trap was worse than "mangled names".** The report is **hierarchical**:
`fir_compute_serial_task_32_s` reports DSP=32/LUT=3728 and its child `..._Pipeline_FIR` reports
DSP=32/LUT=2554 — the parent figure *already contains* the child. Summing every row double-counts
enormously. Only task rows are summed; `_Pipeline_*` descendants are kept as breakdown. The naming
join itself turned out fully derivable from each module's own `KernelTask`
(`task_fn` + `template_args` → `mem_w_stream_framed_done_task_32_8`), so nothing is tabulated by hand.

**Gate (passed), against the real committed report** (ntap=32, samp_w=16, serial, xc7z020):

| | LUT | FF | DSP | BRAM |
|---|---|---|---|---|
| Σ modules | 6690 | 9398 | 32 | 0 |
| integration | 1984 | 1949 | **0** | **2** |
| top (design) | 8674 | 11347 | 32 | 2 |

### The first measurement, and what it already says

* **DSP is perfectly additive.** All 32 DSPs are in `FirCompute` — one per tap — and the integration
  term is *exactly zero*. That is the strongest form of the additive premise, confirmed on the first
  measurement rather than assumed.
* **BRAM is entirely integration.** No module reports any; the design's 2 blocks are inter-task
  FIFOs. The tap storage went to LUT/FF via `ARRAY_PARTITION` — the storage-mapping discontinuity
  predicted in the priors discussion, showing up immediately. A per-module BRAM prior would predict 0
  here and be right; the BRAM belongs to the **interface** term (E1's second term), not the module one.
* **Integration is ~23% of LUT and ~17% of FF.** Not negligible: Σ-modules alone would underestimate
  the design by a quarter, which settles the question of whether the third term is needed.
* `entry_proc` (the DATAFLOW entry process) is correctly unclaimed — real cost, no module's.

### B2 — the sweep ✅
`ntap ∈ {8,16,32}` × `samp_w ∈ {8,12,16,24}` × `unroll_lane ∈ {F,T}` = 24 points, `mem_dwidth=32` and
`samp_i=2` held fixed. Driver: `examples/fir_block/fir_block_sweep.py` (work-tier platform
`calib/work/zynq7020_fir_sweep`, incremental + `--resume`, `--dry-run` pre-flight).

**Gate (passed): 24/24, zero failures, 20.5 minutes** (~51 s/point). 100 records over **30 distinct
module configurations**.

#### The reuse claim, measured

| module | distinct keys over the grid |
|---|---|
| `FirCompute` | 24 — moves with every knob |
| `FirCmdRx` | 4 — sees only `samp_w` |
| `MemRStream` | **1** — sees neither `ntap` nor `samp_w` |
| `MemWStream` | **1** |

24 whole-top syntheses produced 96 module measurements over only 30 distinct configurations. The two
memory modules were characterized **once** and reused across all 24 design points. This is the
projection property from A1 paying off in measured syntheses rather than in argument.

#### The integration term is *exactly* constant

Every one of the 24 points returned the **identical** third term:

```
integration = {lut: 1984, ff: 1949, dsp: 0, bram: 2}
```

Not approximately — one unique value across every `ntap`, every `samp_w`, and both realizations. The
glue does not depend on the compute it wraps.

Stated carefully, because the sweep held `mem_dwidth` fixed and the boundary is defined by *that*, not
by `samp_w`: what this shows is that the term depends on the **boundary structure and not on the
modules inside**. That is the substantive claim E1 needs, and it suggests the third term may be a
*lookup keyed on boundary structure* requiring no fit at all. Confirming it requires varying
`mem_dwidth` — worth one small extra sweep before E1 commits to that.

#### DSP is a two-sided step function

The measured DSP counts, and the reason this example was the right pilot:

| serial | w=8 | w=12 | w=16 | w=24 |     | unroll | w=8 | w=12 | w=16 | w=24 |
|---|---|---|---|---|---|---|---|---|---|---|
| ntap=8 | 5 | 8 | 8 | 16 | | ntap=8 | 16 | 16 | 16 | 16 |
| ntap=16 | 9 | 16 | 16 | 32 | | ntap=16 | 32 | 32 | 32 | 32 |
| ntap=32 | 17 | 32 | 32 | 64 | | ntap=32 | 64 | 64 | 64 | 64 |

* **serial** — `w=12,16` → `ntap`; `w=24` → `2·ntap` (24 bits exceeds the DSP48E1's 25×18, so a
  multiply takes two); `w=8` → `ntap/2 + 1`, because HLS **packs two 8-bit multiplies into one DSP**.
* **unroll** — `2·ntap` flat, independent of width.

So the prior is a step function that moves in *both* directions with width — a packing win below,
a splitting cliff above — which is stronger evidence for the encode-the-physics approach than a clean
monotone fit would have been. The `+1` at `w=8` is unexplained and should be absorbed as a residual,
not rationalized.

#### A design finding, for free

At `samp_w=8` the serial kernel uses **5 DSPs against the unrolled kernel's 16** — a 3.2× difference
for the same function. At `samp_w=24` both use `2·ntap` and unrolling costs no DSPs at all. So the
right realization choice *inverts* with sample width. That is exactly the kind of non-obvious result
the DSE paper needs, and it fell out of the first sweep.

## Phase C — the standalone module harness

### C1 — `ComponentFixture.synth_top()`
Generalize the fixture from timing-only to also render a **self-contained HLS project with the single
module as top**, reusing `composite_top_spec` on a one-module graph. Keep the project — it is the
IP-export unit if the Vivado IPI path is ever taken.

Pilot on `FirCompute` **specifically because it is stream-in/stream-out**, which sidesteps a known
constraint: `hls::task`/`ap_ctrl_none` cannot carry `m_axi`, so a standalone top for a *memory* module
needs a DATAFLOW top instead. That is to be **verified, not assumed**, when `MemRStream` gets a
fixture — not now.

C1 also **resolves the boundary-port depth question** above, against measured numbers rather than
argument.

**Gate:** `FirCompute` synthesizes standalone at 4 param points; records land under its own key; the
standalone-vs-in-composite delta is reported (not required to be zero — it is data about term 3).

## Phase D — the model

### D1 — analytical priors, zero fitting ✅

`examples/fir_block/fir_block_resource.py` + `tests/examples/test_fir_block_resource_prior.py`
(32 tests), plus the `ResourceModel` base and its `Lookup` / `Prior` kinds in
`waveflow/calib/resource_model.py` (17 tests).

**Gate (passed): 24/24 exact on DSP and BRAM, with zero fitted parameters.** Not "within a tight
bound" — exact at every measured point.

The formula is `DSP = n_mult × dsp_per_mult(samp_w)`, from two facts and nothing else:

* **DSP48E1 geometry** (25×18 signed): `samp_w ≤ 8` → 0.5 (two multiplies share a DSP);
  `≤ 18` → 1; `≤ 25` → 2 (one operand exceeds the 18-bit port, so the product splits).
* **the kernel's multiplier count**: serial holds one window → `NTAP`; the unrolled body declares
  *"LW independent windows -> LW*NTAP multipliers"* → `NTAP × LW`, with `LW = mem_dwidth // samp_w`.

#### The unrolled plateau is two effects cancelling

The raw measurements show the unrolled kernel at `2·NTAP` DSPs at *every* sample width, which looks
like an arbitrary plateau and would have been tempting to hard-code. It is not arbitrary — lane count
falls as width rises while DSP-per-multiply rises, and over this device's step boundaries they cancel
exactly:

| `samp_w` | `LW = 32//w` | DSP/mult | product |
|---|---|---|---|
| 8 | 4 | 0.5 | 2·NTAP |
| 12 | 2 | 1 | 2·NTAP |
| 16 | 2 | 1 | 2·NTAP |
| 24 | 1 | 2 | 2·NTAP |

Hard-coding the plateau would be indistinguishable on this grid and **wrong the moment `mem_dwidth`
changes** — at `mem_dwidth=64, samp_w=16` the product is 4, not 2. That is a test.

#### The one unexplained thing

A constant `+1` in the serial packed case (`samp_w ≤ 8`): the prior gives `NTAP/2`, the measurement is
`NTAP/2 + 1`, at every `NTAP ∈ {8,16,32}`. Constant, so it is one multiply that failed to pair rather
than a wrong law. It is kept as a **named constant** (`SERIAL_PACK_CORRECTION`) rather than folded
into the formula, so it stays visibly unexplained instead of being dressed up as physics.

BRAM's prior is `0` — asserted, not defaulted: the arrays carry `ARRAY_PARTITION` from their
`add_state` declaration, so a future configuration that *does* spill into block RAM shows up as a
prior failure rather than passing unnoticed.

### The `mem_dwidth` test — the interface term is boundary-only ✅

Four runs (`ntap=32, samp_w=16`, both realizations, `mem_dwidth ∈ {32, 64}`):

| | serial | unroll |
|---|---|---|
| `mem_dwidth=32` | 1984 / 1949 / 2 | **1984 / 1949 / 2** |
| `mem_dwidth=64` | 2356 / 2057 / 4 | **2356 / 2057 / 4** |

*(LUT / FF / BRAM.)* Identical across realizations; different across boundary width, with BRAM
doubling as the adapter buffers widen. Both halves of the prediction hold, so E1's third term is
confirmed as a **function of boundary structure** and can be characterized per platform rather than
fit per design.

### D2 — learned LUT/FF residual
Ridge (or a small GP where uncertainty is wanted) on top of the priors, with `in_hull` actually
populated from the fitted corpus.

**Gate:** held-out points inside the hull meet tolerance; points outside are **reported as outside**
rather than silently guessed.

## Phase E — composition

### E1 — the three-term sum

Σ modules + Σ interfaces + shell. **B2 changed how this should be built**: the third term is not a
residue to be fit, it is a nameable set of RTL in one-to-one correspondence with the elaborated
interface graph.

`ModuleInformation` reports only what HLS derives from *C functions* — the task modules, their
`_Pipeline_*` loop sub-modules, `entry_proc`, and the top. Everything generated from *interface
pragmas and dataflow channels* gets no row, and that is exactly the integration term:

| unreported RTL | derived from | on `fir_block` |
|---|---|---|
| `gmem<n>_m_axi` | one per `m_axi` boundary port | 2 |
| `fifo_w<W>_d<D>_S` | one per **internal** task-to-task channel | 3 |
| `control_s_axi` | the ap_ctrl / AXI-Lite block | 1 |
| `entry_proc`, `regslice_both`, `sparsemux_*`, flow control | the DATAFLOW shell | fixed |

Note the FIFOs are *internal* — the term is a function of the whole interface graph, not just the
external port list. So:

```
interface term = Σ adapter_cost(kind, width)   over external ports
               + Σ fifo_cost(width, depth)     over internal channels
               + fixed shell
```

Characterize `adapter_cost` / `fifo_cost` **once per platform** — the same move `BusCalib` makes for
the bus law — and a new design's glue becomes a lookup over its own elaborated graph with no
per-design fit. Waveflow already has the graph at elaboration time.

**The falsifiable prediction, and the run to make first.** The term was constant across all 24 B2
points because `mem_dwidth` was held fixed, so no adapter or FIFO ever changed width. Varying
`mem_dwidth` **must** move it. That is a cheap, decisive test and it should run before E1 commits to
this structure.

**Two report quirks to carry forward.** Vitis's own two totals disagree slightly — the `fir_block`
row reads 2 LUT above `AreaEstimates` (FF/DSP/BRAM match exactly); `AreaEstimates` is used as the
design total. And HLS adds FIFO slack beyond the declared depth: three channels declared `depth=2`
emitted depths 2, 3, and 5, so `fifo_cost` must key on the *emitted* depth, not the Python one.

### E2 — held-out validation ✅

`tests/examples/test_fir_block_compose.py` (12 tests). Only per-module figures train anything; the
**design totals are held out by construction**.

| counter | whole-design error | rank correlation vs synthesis |
|---|---|---|
| DSP | **24/24 exact** | 1.000 |
| BRAM | **24/24 exact** | — |
| LUT | 3.2% mean, 8.6% worst | 0.950 |
| FF | 2.8% mean, 8.7% worst | 0.990 |

Plus the decision an exploration actually makes: the DSP-minimal design is identified correctly.

**Two honesty notes, written into the test rather than left implicit.**

The whole-design errors are markedly *better* than the compute module's own held-out error (9.8% /
24.8%), and not because the model is good — because the interface term and the three static modules
are **exact** and dilute the one fitted module. Most of this design is known rather than predicted,
so the total flatters the model. Quoting 3.2% as "the model's accuracy" would be wrong.

The suite leads with **decision fidelity** — rank correlation, and correctly picking the extremum —
because that is the claim the numbers support and the one exploration needs. A model with 10% LUT
error still makes every correct choice when candidates are well separated, and that is a more
defensible claim at review than a regression table.

## Explicitly not now

* **No agent tools / MCP surface.** Drive everything from a plain Python script over the store. The
  action set is a thin adapter once the API is right; designing the actions first would bake
  LLM-shaped assumptions into the store.
* **No Vivado synthesis yet** — but `source` exists from A2, so it is a data addition. After E2, add
  one *measurement* stage: sample the HLS-estimate-vs-Vivado gap at ~6 `fir_block` points. If it is a
  stable ratio, model it; if it is not, that is a finding and it escalates. Do not assume either way.
  (HLS LUT/FF estimates are commonly ~2× off; DSP/BRAM are close.)
* **No IPI / OOC / netlist reuse.** The whole system is one HLS project today —
  `composite_top_spec` emits a single `.cpp` and one TCL builds the top, so there is no module-level
  unit to reuse at the C-synth level. Genuine netlist reuse requires each module to become its own
  HLS IP with integration in Vivado IPI (the deferred SALSA path). **DSE does not need it**: the
  additive model is precisely how it is avoided. C1 keeps the option open at zero extra cost.
* **No uncertainty-driven active sampling loop.** That is DSE-time behavior. A3 makes it possible, D2
  makes it meaningful, and it gets built when CG needs it.

## Traps

* **A stale cache entry that reports success is worse than no cache.** We have been bitten already: a
  stale `rtl_fir_block.f` beside a cached `xsimk.dll` makes an XSI run go green while proving nothing,
  which is why `fir_block_build.py` regenerates the file list after every csynth. Under a cache shared
  across designs *and* parameter points, that failure mode goes from occasional to constant. Every
  cached artifact stores the hash of the inputs it came from, and the loader compares before use.
  Content-addressing here is a safety property, not an optimization.
* **The csynth report is hierarchical — this is the big one.** A task row's figure already contains
  its `_Pipeline_*` children. Summing every row double-counts (measured: it nearly doubles
  `FirCompute`). Sum task rows only; keep sub-blocks as breakdown.
* **HLS module names are mangled**, but derivably: `<task_fn>_<template args>` plus a tool suffix, both
  halves available from the module's `KernelTask`. An unmatched module must fail loudly — silently
  dropping one shrinks Σ-modules and *inflates* the integration term, which reads as "modules are well
  modelled, glue is expensive" when the truth is "we lost a module".
* **`~0` in the report.** `csynthparse` already keeps non-numeric resource cells as strings. The store
  must normalize (`~0` → 0) at the boundary or the arithmetic breaks downstream.
* **Standalone ≠ in-composite.** HLS shares and inlines across task boundaries. A standalone module
  number is not that module's contribution to the composite. Never report the sum of standalone
  numbers as a whole-design prediction without the integration term.
* **Global parameters destroy cache reuse.** `mem_dwidth` and the clock touch every module, so every
  change invalidates every key. Keep them in a coarse outer loop, not the inner search. B2 holds
  `mem_dwidth` fixed for exactly this reason.
* **FIFO depth is part of the key, and that is deliberate.** A harness that binds a module's streams
  at a different depth than the composite does is building *different hardware*, and its records
  legitimately will not join. C1's harness must reproduce the composite's depths on purpose, not by
  luck. See [the open decision](#open-decision--boundary-port-depth-a-c1-blocker).
* **Collinear sweeps.** Same trap the timing fixtures document: vary features independently (a grid,
  not a diagonal), or the coefficients cannot be separated. `LW = mem_dwidth/samp_w` means `samp_w`
  moves two things at once — the prior must express that, and the residual must not try to re-fit it.

## Related

* [`paper_cg_dse_vision.md`](paper_cg_dse_vision.md) — the north-star paper; this is its resource half.
* [`add_state.md`](add_state.md) — the `fir_block` pilot's origin, including the "resource-vs-bitwidth
  probe" intent and the DSP packing cliff.
* [`interleaver_timing_overhaul.md`](interleaver_timing_overhaul.md) — the timing half's most recent
  shape; the fixture/measured-fit precedent this mirrors.
