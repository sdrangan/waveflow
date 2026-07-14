# CodegenSource / SeqTB factoring — design-options report

**Status:** Sub-phase C of Phase 2 (`plans/exec_model_classes.md`) stopped at the design gate. The
clean factoring the plan sketches is **feasible but is option (c) — a larger refactor**, not the
drop-in (a). Per the sub-phase's own instruction ("if (b) or (c): STOP and write the options; do not
force a messy split"), **no code was changed**. This report is the deliverable; it recommends a path
for a follow-up gated PR.

## The goal (recap)

`HwTestbench` is a sequential codegen *source*, not a hardware object: `PolyTBHls` (`poly.py:335`) uses
**no endpoints, no `HwParam`, no `run_proc`** — only `main()` + `cpp_kernel_name`. It is a
`HwComponent` today solely because the extractor gate is `issubclass(HwComponent)`
(`hwcodegen.py:777`). The plan wants:

```
CodegenSource                 cpp_kernel_name · cpp_namespace · param_supports · HwParam/HwConst machinery
├── HwComponent(…, Component)  + endpoints · run_proc · control_mode · SimObj lifecycle
└── SeqTB                      + main() · data_dir      (NOT a SimObj)
```

…and to loosen the gate to `issubclass(CodegenSource)`.

## The finding (why it is (c), not (a))

The open question was whether `CodegenSource` can carry the param machinery **without**
`Component`/`SimObj`. Two probes settled it:

1. **A field-less `CodegenSource` cannot be constructed the way the codebase constructs testbenches.**
   Both `elaborate()` (`elaborate.py:85` → `cls(name=…, sim=ElabContext())`) and the existing tests
   (`test_hw_testbench.py:51/69/79/87/219/267/287` → `Tb(name=…, sim=Simulation())` — **15 sites**)
   pass **`name=` and `sim=`**. A `@dataclass CodegenSource` with no fields rejects both
   (`unexpected keyword argument 'name'`). So `CodegenSource` must at minimum own a `name` field, and
   *something* must absorb the `sim=` kwarg.

2. **The `HwParam` machinery itself is NOT SimObj-dependent.** A `NamedObject`-based `CodegenSource`
   (so it owns `name`) with `_wrap_hw_params` + the immutability `__setattr__` + a `__post_init__` that
   conditionally chains `super().__post_init__()` **constructs sim-lessly and runs its `__post_init__`
   fine**. So the machinery *can* live above `Component` — the blocker is purely the **construction
   contract** (`name`/`sim`) and the **hierarchy plumbing**, not the param logic.

Conclusion: the fully-clean (a) ("`CodegenSource` with none of the SimObj surface") is impossible
because construction is defined in terms of `name`+`sim`. A genuinely clean split **is** achievable
(option C below) but it changes `elaborate`'s construction contract, reparents the whole
`HwComponent` hierarchy through a diamond, and must be re-verified byte-identical across **every**
kernel — that is a larger refactor, deliberately out of scope for a single byte-identical sub-phase.

## Options

### Option A — field-less `CodegenSource` (REJECTED, impossible)
A `CodegenSource` carrying only ClassVars + param machinery, no `name`/`sim`. **Rejected:**
construction (`elaborate` + 15 tests) passes `name=`/`sim=`; a field-less base rejects them. Proven by
probe 1.

### Option B — marker base above `HwComponent` (shallow, low risk, partial)
- `class CodegenSource: …` (a thin base holding `cpp_kernel_name`/`cpp_namespace`/`param_supports` as
  the *declared* contract), `class HwComponent(CodegenSource, Component)`, loosen the gate to
  `issubclass(CodegenSource)`.
- `HwTestbench` **stays `HwComponent`-derived** (optionally renamed `SeqTB` with a `HwTestbench`
  alias), so it remains a `SimObj`.
- **Blast radius:** tiny — `HwComponent`'s bases + the gate line + one new near-empty class. No
  construction change, no test changes.
- **Byte-identical:** trivially (no `__init__`/field/`__post_init__` change).
- **What it buys:** a named `CodegenSource` concept and a gate expressed in its terms.
- **What it does NOT buy:** the plan's actual structural goal. `SeqTB` is still a `SimObj` (still owns
  empty `endpoints`/`sub_comps`/`interfaces`, still registers with a throwaway sim during elaboration).
  Since an `HwComponent`-derived `SeqTB` already passes `issubclass(HwComponent)`, the gate change is
  **cosmetic** here. Honest verdict: a placeholder, not the refactor.

### Option C — `NamedObject`-based `CodegenSource`, `SeqTB` off `SimObj` (the real split)
The end-state the plan wants. `CodegenSource(NamedObject)` owns `name` + the param machinery;
`SeqTB(CodegenSource)` is sim-less; `HwComponent(CodegenSource, Component)` keeps the concurrent
machinery.

Concrete changes:
1. **New `waveflow/hw/codegen_source.py`** — `CodegenSource(NamedObject)` with `cpp_kernel_name`,
   `cpp_namespace`, `param_supports` ClassVars + `_wrap_hw_params`, the immutability `__setattr__`, and
   a `__post_init__` that chains `super().__post_init__()` **only if present** (so `SeqTB` stops at
   `NamedObject`, `HwComponent` runs the full `Component`/`SimObj` chain).
2. **`hw_component.py`** — `HwComponent(CodegenSource, Component)` (a **diamond**: both derive
   `NamedObject`); move the param machinery to `CodegenSource`; **re-anchor two hard-coded
   `if klass is HwComponent` breaks → `is CodegenSource`** — `_wrap_hw_params` (`:315`) and
   `SynthContext.from_component` (`:191`, the only caller is `hwgen.py:1332`).
3. **`hw_testbench.py`** — `SeqTB(CodegenSource)` carrying `main()`/`data_dir`/`_is_testbench`; keep
   `HwTestbench = SeqTB` as a deprecated alias.
4. **`elaborate.py:85`** — construction must stop unconditionally passing `sim=`. Options: pass `sim`
   only when `issubclass(comp_class, SimObj)`, or give `elaborate` a construction protocol. Both the
   real path and the param-purity double-build go through `_build`, so this is one spot but a **change
   to the elaboration construction contract** (Phase 0 surface).
5. **`hwcodegen.py:777`** — gate `issubclass(HwComponent)` → `issubclass(CodegenSource)`.
6. **~15 test construction sites** in `test_hw_testbench.py` drop `sim=` (a `SeqTB` no longer takes it).

- **Blast radius:** the construction path of **every** `HwComponent` (diamond MRO + moved
  `__post_init__` + moved field/machinery), `elaborate`'s contract, the extractor gate, and the tb
  tests. Diamond dataclass field-merge (`name` via `NamedObject` once; `sim`/`endpoints` via
  `Component`) must be verified — Python linearizes `NamedObject` once, but dataclass field order and
  `__post_init__` dispatch across the diamond need care.
- **Byte-identical risk:** **real and hierarchy-wide.** Every kernel component's construction and
  `SynthContext` derivation change; must re-verify byte-identical for poly / hist / simp_fun / vmac /
  interleaver / mem_stream **and** every `*_tb.cpp`, not just `poly_tb.cpp`.
- **What it buys:** the plan's structural goal — `SeqTB` is a first-class codegen source, not a
  hardware object; no throwaway `SimObj` registration; the gate is meaningful.

## Recommendation

**Do Option C as its own dedicated, gated PR — not bundled into this byte-identical sub-phase.** The
factoring is sound (probe 2 shows the param machinery runs standalone), but it changes the construction
contract of the entire component hierarchy and the Phase-0 `elaborate` surface, so it needs the same
before/after byte-identical sweep across *all* kernels and testbenches that Gate A/B applied to their
narrow slices — that is a gate of its own, not a rider on Sub-phase C.

Concretely:
- **Now:** stop at this report (done). Do **not** ship Option B as a consolation — an
  `HwComponent`-derived `SeqTB` already satisfies the gate, so B is cosmetic and would bake in a
  misleading "CodegenSource" that isn't the real base.
- **Next (recommended follow-up gate "C-real"):** implement Option C end-to-end with the byte-identical
  sweep, in this order to de-risk: (1) land `CodegenSource(NamedObject)` + move machinery + re-anchor
  the two breaks + `HwComponent(CodegenSource, Component)`, verify **all kernels** byte-identical with
  `HwTestbench` still `HwComponent`-derived (no behavior change yet); (2) then reparent
  `SeqTB(CodegenSource)` sim-less + the `elaborate` conditional-`sim` + the gate loosening + the ~15
  test edits, verify **all `*_tb.cpp`** byte-identical. Two internal checkpoints, one PR.

## Evidence (probes, not committed)

- Probe 1 — field-less `CodegenSource(name=…, sim=…)` → `TypeError: unexpected keyword argument
  'name'` (construction needs a `name` field; `sim=` needs an absorber).
- Probe 2 — `CodegenSource(NamedObject)` + param machinery, `SeqTB(name="tb")` sim-less → constructs,
  `__post_init__` runs, `_hw_construction_complete` set. The machinery is not SimObj-dependent.
- Facts: `HwComponent` is **not** `@dataclass`-decorated (inherits `Component`'s `__init__`);
  `Component → SimObj → NamedObject`; `if klass is HwComponent` at `hw_component.py:191` (SynthContext)
  and `:315` (_wrap_hw_params); 15 `sim=`-passing tb constructions in `test_hw_testbench.py`; the sole
  elaborate construction site is `elaborate.py:85`.
