# Plan: the codegen `(subject × target)` family — `generate` / `check`

## Context

Codegen today is a set of related roles with no common shape: generate a synthesizable Vitis kernel,
generate a Vitis testbench, (future) generate SystemC, (future) generate an XSI BFM — **and** the missing
one: *ask whether any of those would succeed, without doing them.*

There is **no** extractability predicate. Every rule lives inside the extractor and **raises**
`SynthesisError` (10+ raise sites in `hwcodegen.py`): implicit `self.X` capture
(`_validate_no_implicit_capture`, allow-list = endpoints / `RegMap` / `AXIMMQueue` / `HwParamValue` /
`DataSchema` *types* / `@sim_only` / `@synthesizable`), non-synthesizable calls, forbidden statement
shapes, bad yields. And **nothing checks for concurrency at all** — a `main()` that spawned
`env.process(...)` would fail with "Non-synthesizable call", not "this is concurrent, use SystemC".

**The unifying move.** These are not six functions; they are **one dispatch, two modes**:

```
generate(subject, target)  =  validate(subject, target) + emit(...)
check(subject, target)     =  validate(subject, target)          -> (ok, err_msg)
```

Factor `validate` out of each path and `check` is free — and, crucially, **cannot drift** from what
`generate` actually accepts. A try/except wrapper around extraction would be a *shadow* of the rules;
this *is* the rules. The **target** axis is the seam `codegen_dispatch.py` was designed for
(`CodegenPath.kind` exists so one subject can lower to `hls::task` *and* SystemC).

**Decision on the base class (settled in discussion).** Do **not** add a `CodegenSource` base yet. The
per-class facts we need (a target list, the entry) are **data**, and the codebase already declares such
data duck-typed — `cpp_kernel_name`, `cpp_namespace`, `param_supports`, `control_mode`, `_is_testbench`
are ClassVars that `build/` reads via `getattr`, and `codegen_path()` already dispatches across
`HwComponent` **and** `SeqTB` with no shared base. A base would add a shared default, a facade
(`MyTB.check(...)`), and a type — nice, not load-bearing — at the cost of a `hw/ -> build/` lazy-import
inversion. **We added `CodegenSource` once on *guessed* content (the `HwParam` surface) and it was
wrong.** Build the family first; let it prove what is genuinely per-class:

- **data only** (targets, entry) → ClassVars suffice, no base;
- **real behavior** (a class must *override how it validates* for a target) → a base has earned it, and
  it is then a ~5-line facade we add with evidence.

## Stage 1 — the target axis (no behavior change)

**Target vocabulary — drawn verbatim from the four rows of
[`docs/guide/flows/index.md`](../docs/guide/flows/index.md)**, so the code and the docs use one set of
words:

| Flow | DUT target | TB target |
|---|---|---|
| 1 · Control-driven kernel | `control_driven_kernel` | `sequential_vitis_tb` |
| 2 · Free-running, sequentially driven | `free_running_kernel` / `composite_kernel` | `sequential_xsi_tb` |
| 3 · Free-running, concurrently driven | *(same as Flow 2)* | `concurrent_systemc_tb` |
| 4 · Full system, on hardware | `bitstream` | — (host software) |

Implement **`control_driven_kernel`** and **`sequential_vitis_tb`** only; declare the rest as known
names that are not yet reachable.

> **The DUT and TB targets are different species — do not oversell the axis.** The TB targets are a real
> choice (one `main()`, three lowerings). The DUT targets are ~1:1 with the class (`HostActivated` →
> `control_driven_kernel`), so for a DUT the axis is a *name and a validation hook*, not a fork. It still
> earns its place: it is what lets `check(cls, "concurrent_systemc_tb")` answer *"not a potential target
> for this kind"*.

Declare per-class targets as a ClassVar, house style (read by `build/` via `getattr`):

```python
class HostActivated(HwComponent):
    potential_targets: ClassVar[frozenset[str]] = frozenset({"control_driven_kernel"})
class FreeRunComp(HwComponent):
    potential_targets: ClassVar[frozenset[str]] = frozenset({"free_running_kernel"})
class CompositeComp(HwComponent):
    potential_targets: ClassVar[frozenset[str]] = frozenset({"composite_kernel"})
class SeqTB(NamedObject):
    potential_targets: ClassVar[frozenset[str]] = frozenset({"sequential_vitis_tb"})
```

> **Why `potential_`, not `supported_`.** "Supported" reads as a guarantee, which would sneak
> synthesizability back onto the class — exactly what removing `SynthComp` was meant to stop (see
> `docs/guide/components/taxonomy.md`: *synthesizability is a codegen/usage axis, not a class fact*).
> `potential_targets` declares **the paths that exist for this kind**; `check()` answers **whether this
> particular component actually makes it**. The class states the kind; the predicate states the fitness.

**Gate:** pure addition — all generated C++ byte-identical.

## Stage 2 — factor `validate` out of `generate`

- Split the extract path so the checks are callable without emitting:
  `generate(subject, target)` = validate + emit; `check(subject, target)` → `(ok, err_msg)`.
- `check` returns the **first** violation with its line and offending name (the existing messages are
  decent). Collecting *all* violations in one pass is a later refinement — shape the return so it can
  grow (`(ok, msg)` → a structured report) without breaking callers.
- **Subject = a class (or instance), not a bare function.** Resolving `self.X` against the allow-list
  needs an *elaborated* component; only the syntactic subset is checkable from a function alone. Take a
  class and `elaborate()` internally, so the call site still reads `check(SimpFunComponent)`.
- **`check` stays user/test/docs-facing.** Leave the codegen path raising as it does (already fail-loud);
  do not call `check` then `generate` (double work).
- **Gate:** byte-identical C++ for all kernels + all four TBs — this stage is a refactor of existing
  rules into a callable shape, nothing more.

## Stage 3 — the sequential gate (**the only new rule**)

Reject **syntactic** concurrency — `env.process(...)` fan-out / spawning — with a message that names the
real fix: *"this testbench is concurrent; it has no straight-line `int main()` lowering — it needs the
SystemC path (Flow 3), not C-simulation."*

> **Honest limitation, to be stated in the docs:** this is a **gate, not a proof**. Semantic
> interleaving (a TB that interleaves writes/reads with the DUT running in between) is undecidable in
> general. We reject the syntactic constructs that certainly imply concurrency; we do not certify
> sequentiality.

**Gate:** no existing kernel or TB trips it.

## Stage 4 — the component-level contract

The thing the docs quote. Built on Stage 2:

> A `HostActivated` synthesizes to a **standalone Vitis kernel** iff
> **(a)** it has no sub-components / internal interfaces, and
> **(b)** its `on_start` passes `check(..., target="control_driven_kernel")`.

Structural rule (a) + body rule (b). Same shape for the other kinds.

### Fold in: the `cpp_namespace` default emits ill-formed C++

Found by the toys (2026-07-15) — the first component to *take* the default. `resolved_namespace`
([`hwgen.py:994`](../waveflow/build/hwgen.py)) resolves `cpp_namespace = None` to the **kernel name**, so
a component with a hook emits `void square(...)` **and** `namespace square { ... }` into one scope. That
is ill-formed C++ (*"redeclared as different kind of entity"*, confirmed against g++), and codegen raises
nothing.

Every real component hand-sets `<kernel>_impl` — `simp_fun_impl`, `poly_impl`, `hist_impl`,
`block_scale_impl`, `mem_r_stream_impl`, `mem_copy_impl`, `fir_impl`. **A 100% opt-out rate is the
evidence: the default is unusable whenever a hook exists**, and it has survived only because nobody has
ever used it.

Two candidate fixes — decide when implementing:

- **Default to `f"{kernel_name}_impl"`.** Makes the common case correct by construction and matches what
  every component already writes by hand. Should be byte-identical for all existing kernels precisely
  *because* they all override it — which the Stage-2 gate would confirm.
- **Fail loud** when the resolved namespace equals the kernel name and a hook is emitted.

They compose (fix the default *and* keep the guard as a `check` rule). This belongs here rather than in
[`toy_examples.md`](./toy_examples.md) because it is exactly what `check(..., "free_running_kernel")`
should catch: **silently wrong output, not a loud failure**. The toys pin the current broken behavior in
a characterization test, so whichever fix lands will fail that test loudly and force the doc update.

## Docs — `guide/comp_codegen` (where this belongs)

- **`extractor.md`** *(the substantive change)* — it already owns "the synthesizable subset … failing
  fast with `SynthesisError`", so it becomes the home of the rules:
  - the rules made **explicit** (the `self.X` allow-list; what is rejected and why);
  - **`check`** presented as *the same rules, callable* — a predicate instead of an exception — with the
    "cannot drift because `generate` runs the same `validate`" point;
  - the **sequential gate** + its honest limitation.
- **`index.md`** — a short framing addition: codegen is **one dispatch, two modes** over
  `(subject × target)`; the target table (`vitis_kernel`, `vitis_tb`, + the future ones) and which kinds
  support which. Forward-link to [`guide/flows`](../docs/guide/flows/) — the sequential-vs-concurrent
  line here **is** the Flow 1–2 vs Flow 3 boundary.
- **`structure.md`** — the **contract** from Stage 4. This page already owns the execution model and
  "where the kernel body comes from", so "when does it lower at all" belongs beside it.
- *(Alternative if the family outgrows `index.md`: a dedicated `targets.md` at nav 7.)*

## Depends on

[`plans/toy_examples.md`](./toy_examples.md) — the tested `Square` (`FreeRunComp`) and `Double`→`Square`
(`CompositeComp`) toys are this plan's **fixtures**: a subject that passes `check`, per kind, plus the
crafted variants that fail. (`simp_fun` already serves as the tested `HostActivated`.) The two plans are
independently reviewable, but land the toys first so the check tests have something real to check.

## Not in scope

- The `CodegenSource` base — revisit **after** the family exists, with evidence (see Context).
- `sequential_xsi_tb` / `concurrent_systemc_tb` / `bitstream` targets — declare the names, don't
  implement them (they are the flows' future work).
- Collecting *all* violations per pass — first-violation is fine for v1.

## Verification

- Run via the venv: `../pysilicon-venv/Scripts/python.exe -m pytest -m "not vitis"`; failures ⊆ the
  documented baseline (`test_build`×9 + `dataschema_poly`×1 + `poly` timing×5 — see
  [[project-test-baseline-failures]]).
- **Byte-identical C++ at every stage** for all kernels and all four TBs (`kernel_files_to_str` /
  `tb_files_to_str`, dict-equal before/after). The family is a refactor; the only new rule is Stage 3,
  which nothing existing trips.
- Tests: `check()` returns `(True, None)` for the real components/TBs; `(False, msg)` for crafted
  violations — an implicit mutable `self.X` read, a non-`@synthesizable` call, and an `env.process`
  fan-out — each asserting the message names the actual problem.
