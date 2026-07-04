# Vectorization: the unified operator API, a guide section, and a worked example

## Goal

Make **vectorization a first-class, taught capability** of PySilicon:
1. The **unified type-preserving operator arithmetic API** across int / fixed / float
   arrays (`c = a*b + c*d`, then explicit `quantize`).
2. A new **`docs/guide/vectorization/` guide section** (the selling point + integer,
   float, and fixed-point vectorization; complex later).
3. A worked **`examples/basic_vec`** — Python golden vector → vectorized Vitis testbench →
   functional bit-match (no timing).
4. **Reorganize the examples** into schema/data patterns *before* interface patterns.

Vectorization — keeping data in numpy arrays end-to-end, with no per-element Python — is
what makes PySilicon's functional simulation **fast while bit-exact**. It's a genuine
differentiator that the codebase already pays for; this effort surfaces and teaches it,
and lays the operator foundation `ComplexField` needs.

## Why

The PySilicon thesis is bit-exact *and* fast. Speed comes from vectorization, and we've
already committed to it architecturally: `FixedField` is integer-backed with a
single-64-bit dtype (not arbitrary-precision object arrays) **specifically to stay
vectorized**, and the operator decision keeps arithmetic numpy-backed underneath. Many
Python fixed-point packages fall back to per-element computation for arbitrary precision
(slow), and RTL/cycle-level Python simulators (e.g. PyMTL) pay per-cycle costs that don't
vectorize over data — a *different abstraction level*. PySilicon's transaction-level,
vectorized-data model gives fast **functional** (bit-exact) sim, with timing handled
separately. The docs should make this tradeoff explicit (a speed/abstraction story, not a
"others are bad" one).

## Relation to other plans (read first)

- **Precedes `ComplexField`** (`plans/complexfield.md`): the operator layer here is
  `ComplexField`'s prerequisite. The **complex-vectorization page** and the `basic_vec`
  complex case are **follow-ons after** `ComplexField` lands (Phase 5, gated).
- **Implements** the project-wide arithmetic decision
  (`project-arithmetic-operators-api` memory): operators primary, sugar over `mult`/`add`,
  explicit `quantize`, `.val` numpy escape.
- One PR, multiple commits (per the single-PR convention).

## Background — what we build on

- **`FixedField`** (integer-backed, vectorized, bit-exact; `mult`/`add`/`quantize` + the
  single-64-bit fail-fast guard + the conformance rig). Merged: PR #53.
- **`DataArray`** is numpy-backed (delegates `shape`/`dtype`/…, `__array__`, `__getitem__`);
  `.val` is the underlying ndarray.
- **The conformance rig** — `build_dag` + `run_dag_cli` + gen→csim→compare-bits
  (`examples/schemas/fixedpoint`), factored for reuse.
- **Schema docs** — fields, datalists, dataarrays, dataunion, fixpoint, **fixp_vector**,
  codegen. (`fixp_vector` moves out in this effort.)
- **Examples today** — interface patterns (regmap, pure_stream, stream_inband, shared_mem,
  mem_queue) + the schema conformance (`schemas/fixedpoint`).

## Design decisions (settled — do NOT re-litigate)

1. **Operator layer (type-preserving, sugar over the functions).** Operators (`+`, `-`,
   `*`) on `FixedField` and numeric `DataArray`, returning **full-precision** results
   (no loss), as sugar over the existing `mult`/`add` (both kept). Rounding stays an
   **explicit `quantize(x, fmt)`**; `.val` stays the **numpy escape hatch**.
   - **int** → growth-aware (`a+b` → `+1` int bit, `a*b` → `Wa+Wb`), reusing the
     **single-64-bit fail-fast >64 guard**.
   - **fixed** → reuse `FixedField`'s format derivation + guard.
   - **float** → numpy passthrough (no growth).
2. **New section `docs/guide/vectorization/`** placed **right after Schema** (renumber the
   subsequent guide sections `+1`). Pages: `index` (the selling point + the two paths +
   when to use each), `integer.md`, `float.md`, `fixed.md` (**the moved `fixp_vector`**).
   **`complex.md` is Phase 5** (after `ComplexField`).
3. **Types vs compute split.** `fixpoint.md` (the *type*) **stays in Schema**;
   `fixp_vector.md` (the *compute*) **moves to Vectorization**, retitled. **Cross-link
   both ways** so the fixed-point story isn't fragmented. Fix inbound links; close the
   Schema nav gap (`codegen` renumbers).
4. **`examples/basic_vec` — the pedagogical front-door.** Python golden vector over **one
   simple op** (e.g. elementwise `a*b + c`) across **int / float / fixed**, → a vectorized
   Vitis kernel → **functional bit-match** (no timing). **Reuses the conformance rig**
   (`build_dag`/`run_dag_cli`/gen→csim→compare). Distinct *intent* from
   `schemas/fixedpoint` (teaching vs rigorous), **shared machinery**.
5. **Examples reorg.** Two families in the examples nav/intro — **schema/data patterns**
   (`basic_vec`, `schemas/*`) **before interface patterns** (regmap → mem_queue). A short
   examples index intro framing both. (Nav/docs ordering + intro; not necessarily moving
   directories.)
6. **Verify-the-snippets discipline.** Every doc code block runs and matches its stated
   output; `basic_vec` passes on **real Vitis** (functional, no soft-skips).

## Working convention

- One commit per phase; push after each. Single PR, multiple commits, own branch.
- After each phase: `pytest tests/hw/ tests/utils/ tests/examples/ -k "not vitis"` green
  (known pre-existing failures aside).
- `basic_vec` verified empirically on real Vitis; docs snippets executed.
- Don't merge — pre-merge pass (links, suite, independent `-m vitis` run of `basic_vec`).

## Phases

### Phase 0: Scope + reference (read-only) — PAUSE for review
Settle: the operator semantics per type (int growth rules, float passthrough, fixed reuse,
the shared >64 guard); the section structure + exact nav placement + the `fixp_vector`
move + cross-links + Schema/guide renumbering; the `basic_vec` design (the op, the types,
the rig reuse, its relationship to `schemas/fixedpoint`); the examples reorg ordering.
Produce the reference before code.

### Phase 1: The operator layer
Operators (`+`/`-`/`*`) on `FixedField` + numeric `DataArray`, sugar over `mult`/`add`,
full-precision growth, single-64-bit fail-fast, float passthrough; `quantize` unchanged;
`.val` escape. Tests: operators equal the underlying functions / the Fraction oracle; int
growth rules; float passthrough; `>64` raises; the existing `FixedField` conformance still
holds. Non-vitis green.
**Commit:** `arith: type-preserving operators on FixedField + numeric DataArray (sugar over mult/add)`

### Phase 2: `examples/basic_vec` — the worked example (milestone)
Pedagogical example: Python golden vector (int/float/fixed) over a simple vectorized op →
a vectorized Vitis kernel → conformance via `build_dag`/`run_dag_cli`; **functional
bit-match on real Vitis**. Reuse the rig; keep it minimal and readable. **MILESTONE: the
vectorization selling point demonstrated end-to-end, bit-exact, on real Vitis.** PAUSE.
**Commit:** `examples: basic_vec — vectorized Python golden vs vectorized Vitis, bit-exact (int/float/fixed)`

### Phase 3: The Vectorization guide section
Create `docs/guide/vectorization/` (`index` + `integer` + `float` + `fixed`[moved
`fixp_vector`]); move `fixp_vector` out of Schema and retitle; cross-link
`fixpoint`↔`fixed`; insert the section after Schema and renumber subsequent guide sections
`+1`; close the Schema nav gap; fix inbound links. Pull worked code from `basic_vec`;
**verify every snippet runs**.
**Commit:** `docs(vectorization): new guide section (integer/float/fixed vec) + move fixp_vector`

### Phase 4: Examples reorganization
Reorder the examples nav so schema/data patterns precede interface patterns; add a short
examples index intro framing the two families. Verify links + nav.
**Commit:** `docs(examples): schema-patterns-before-interface-patterns ordering + intro`

### Phase 5 (follow-on — gated on ComplexField)
`docs/guide/vectorization/complex.md` + a complex case in `basic_vec`, once `ComplexField`
(`plans/complexfield.md`) lands. The complex butterfly (`cmult`/`cadd` over
`std::complex<ap_fixed>`) is the natural climax of the vectorization story and the bridge
to the L1 FFT model.

## Future / out of scope (capture, don't build)
- **`ComplexField`** (`plans/complexfield.md`) — depends on this effort's operator layer.
- **Auto-quantize-on-typed-assignment** (`c[:] = a*b`) — deferred per the arithmetic
  decision; explicit `quantize` for now.
- **The DSE / timing performance story** (cycle/resource models) — separate; vectorization
  here is about *functional* simulation speed, not timing.
