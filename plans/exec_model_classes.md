# Plan: component class hierarchy (execution model × structure) + the elaboration contract

## Status

- **Phase 1 DONE (merged, `3dc5f2c` + `651f880`):** `SynthComp` + `FreeRunComp` (`run_iter`), retrofit of
  `MemRStream`/`MemWStream` + the six interleaver tiles, docs (`taxonomy.md`, `structure.md`,
  `concurrency/python/*`). Goldens byte-identical; construction-time synthesizability check works.
- The design has since **grown substantially** (this rewrite). A new prerequisite — **Phase 0, the
  elaboration contract** — was identified and should land *before* further class work. Everything below
  is design-only until approved.

## The conceptual model: three orthogonal axes + inferred targets

The single-tree taxonomy was an oversimplification. There are **three orthogonal axes the class
declares**, and a fourth that is **inferred**:

1. **Role** (class): synthesizable / testbench / behavioral.
2. **Structure** (class): leaf (has a kernel body) / composite (has sub-components, no body).
3. **Execution model** (class): free-running (`ap_ctrl_none`) / host-activated (`ap_ctrl_hs` + regmap).
4. **Target / verification** (**INFERRED from the realization**, NOT a class): cosim vs. XSI vs. IPI;
   "can be a Vitis synth top"; "can be a SystemC sim top".

Execution model ⊥ structure: a **composite can be host-activated** (a top-level regmap → a *start-adapter*
tile, the mirror of the memory wrapper, pushing a start token into a free-running network below). So
"composite" is not an execution-model sibling of "free-running".

**Capability rings** (why "synthesizable" is a capability, not the axis):

```
PySim-able            every SimObj
└── SystemC-able      any concurrent process → SC_THREAD          ← "free-running" lives here
    └── Synthesizable  DTLP-clean synth subset → hls::task         ← a SUBSET
```

Most free-running processes can generate SystemC; **a subset** can also be real `hls::task` hardware. A
BFM driver / signal source / behavioral model is free-running + SystemC-able but **not** synthesizable.

**The `run_proc` trichotomy** (from `simobj.py:87` — `run_proc()` returns a generator *or* `None`):

| `run_proc()` returns | State | Used by |
|---|---|---|
| `None` (default) | **passive** — no process scheduled | `CompositeComp`, any pure container |
| a generator that **returns** | active, terminating | a bounded process; `SeqTB`-style `main()` |
| a generator that **loops forever** | active, free-running | `FreeRunComp` (`while True: run_iter()`) |

This trichotomy resolves the composite lifecycle question: a composite is **passive** (`run_proc → None`);
its children (`add_comp`) are independent scheduled SimObjs. No busy loop, no "must not terminate" — there
is simply no process to kill.

## The class hierarchy (target shape)

```
CodegenSource                 what the extractor really needs: cpp_kernel_name, params, param_supports
├── HwComponent               a concurrent object (run_proc → None passive | generator active)
│   ├── FreeRunComp           run_proc = while True: run_iter()   — free-running leaf   [DONE]
│   │       caps: SystemC-able (usually)  ⊃  synthesizable (subset)
│   ├── CompositeComp         add_comp children · NO run_iter · run_proc → None (PASSIVE) — structural
│   │       codegen = composite_top_spec; execution model follows its boundary (regmap ⇒ host-activated)
│   └── HostActivated         owns a VitisRegIF · on_start (pends on ap_start)           — invocation
└── SeqTB (= today's HwTestbench)   sequential main() · NOT a HwComponent (no concurrency) — cosim driver

ThreadTB                       a composite (add_comp of SC_THREADs) → SystemC sim top      — XSI driver
```

Key moves vs. Phase 1:
- **`CompositeComp` is a passive *sibling* of `FreeRunComp`, not a subclass** (no `run_iter`; its
  `run_proc` returns `None`; a composite is-not-a free-running-leaf).
- **`SeqTB` moves off `HwComponent`.** Verified: `PolyTBHls` uses **no** endpoints, **no** `HwParam`, **no**
  `run_proc` — only `main()` + `cpp_kernel_name`. It's a `HwComponent` today solely because the extractor
  is gated on `issubclass(HwComponent)` (`hwcodegen.py:777`). Factor a **`CodegenSource`** base (what the
  extractor actually needs) out of `HwComponent`; `SeqTB(CodegenSource)` is a sequential codegen source,
  not a hardware object. Loosen the gate to `issubclass(CodegenSource)`.
- **`ThreadTB` stays composite-like** (`add_comp` of concurrent processes) — it needs the concurrent
  machinery, so it is `HwComponent`-derived, not a `CodegenSource`-only class. Its members are
  free-running processes, a subset synthesizable (the DUT tiles); the SystemC generator lowers all of
  them to SC_THREADs.
- **`SynthComp` becomes a capability, not a fixed base.** Today `FreeRunComp(SynthComp)` bakes in
  "free-running ⇒ synthesizable", which `ThreadTB`'s non-synthesizable members break. When SystemC-gen
  lands, invert: `FreeRunComp` is the general free-running base; **synthesizability is a declared/checked
  capability** (today's `SynthComp` construction check *is* that capability check). Not urgent — every
  free-running component that exists *is* synthesizable — but do not deepen the assumption.

### Targets are inferred (not classes)

- **`vitis-synth-top`** ⇐ synthesizable **and** a standalone-top boundary → exportable as Vitis IP.
- **`sc-top`** ⇐ a SystemC-representable composite (`ThreadTB`).
- **verification method** ⇐ *does the design contain a free-running `hls::task` network?* → **XSI**;
  else a single cosim-able kernel (`ap_ctrl_hs`, possibly `#pragma HLS DATAFLOW`) → **Vitis cosim**.
  A host-activated **leaf** cosims; a host-activated **composite of `hls::task` tiles** does **not** (the
  internal free-running `m_axi` tasks are the blocker, regmap top notwithstanding) — but the *same
  accelerator* realized as a single DATAFLOW kernel *would* cosim. So it's the **realization**, not the
  class, that fixes verification. (See `overview/targets.md`, `build/`; multi-block precise cosim behavior
  is the Vivado-IPI/system-flow's domain to settle empirically.)

## Phase 0 — the elaboration contract (PREREQUISITE; do before more class work)

Codegen must depend on the **class + parameters**, not a specific instance. Today it "plays a risky
trick": it instantiates the class with placeholders to read the instance-built topology —
`comp_class(name="_codegen", sim=Simulation(), **overrides)` at `hwcodegen_steps.py:75`,
`hwcodegen.py:745`, and `hwgen.py:1195-1198` (the latter already yields **one elaboration per param set**,
which is what `param_supports` is). This *is* HDL-standard elaboration; it is just **scattered,
sim-coupled, and unenforced**.

**The invariant that fixes it:** a component's **structure — endpoints, sub-components, interfaces — is a
pure function of its compile-time parameters (`HwParam`/`HwConst`)**; name, `sim`, and runtime data are
elaboration context and must not affect structure. If that holds, codegen `= elaborate(class, param_set)
→ structure → C++`, one output per param-set, instance-independent, and any real instance with those
params matches the generated C++ by construction.

**Precedent — DataSchema already does this**, the *declarative* way: structure *is* class attributes
(`elements`, `element_type`, `max_shape`) + classmethods (`nwords_per_inst(cls, word_bw)`), so codegen
reads the class directly with no instantiation. DataSchema gets param-purity **for free** (structure
literally is class attributes). `HwComponent` builds structure *imperatively* in `__post_init__`, so it
must **enforce** by contract what DataSchema gets by construction. Same principle, secured differently.

**Deliverables:**
1. **One `elaborate(comp_class, param_set)` entry** — construct with the target params, return the
   structure (endpoints, sub-comp graph, interface bindings, boundary). Replace the three scattered
   `cls(name="_codegen", sim=Simulation())` sites; route all codegen (`kernel_files_to_str`,
   `composite_top_spec`, `extract_kernel`) through it.
2. **A sim-free elaboration context** — a null/elaboration stand-in for `Simulation()` so codegen never
   spins a real sim to read ports. Favor DataSchema's idiom: where a structural quantity can be a
   **classmethod** (param → value, e.g. block counts), make it one, shrinking what must be elaborated.
3. **Param-purity enforcement — the real safety fix.** Determinism check: `elaborate(cls, params)` twice
   → assert **identical** structure. Structure that depends on non-param state fails loudly at codegen.
4. **Key codegen by `(class, param-set)`**, not instance — make the `param_supports` behavior universal.

**Why first:** the class refactor builds on this. `CompositeComp`'s entire value is "structure from
`add_comp`" — only safe if that structure is param-pure and elaborated per param-set. `CodegenSource` is
*precisely* "the thing `elaborate()` accepts" — so defining the elaboration contract **is** defining
`CodegenSource`. Doing the classes first just bakes the unenforced trick deeper.

## The testbench branch (generation is the point)

- `<kernel>_tb.cpp` is **generated**, not handwritten: `HlsCodegenStep(is_testbench=True)` →
  `tb_files_to_str` (`hwgen.py:1443`) → `_testbench_cpp` (`hwgen.py:1971`), extracting `main()` via
  `extract_testbench` (`hwcodegen.py:1289`). So `SeqTB` already *is* "a component that generates a
  simulation top" — for the cosim rung.
- Both a `SeqTB` and a future `ThreadTB`/BFM derive from the **DUT's interface**: `main()` names the DUT's
  endpoints; a composite declares its `boundary` (port name · kind · bundle). So the DUT's endpoint /
  boundary declarations are the **shared root** for the kernel signature, the sequential TB call, and the
  BFM sim-top. The handwritten `il_bfm_tb.cpp` is the obvious next thing to generate (walk the boundary
  spec, emit a driver per port kind) — that's `ThreadTB`/`SystemCTestbench`.

## Execution-model machinery (unchanged from Phase 1, still valid)

- `ControlMode` enum (`hw_component.py:160`): `AUTO` / `FREE_RUNNING` (ap_ctrl_none) / `PER_INVOCATION`
  (ap_ctrl_chain). `FreeRunComp` sets `FREE_RUNNING` explicitly, skipping the `AUTO` "`while` at the root"
  inference. `LoopComp` (ap_ctrl_hs persistent) needs a *new* mode — deferred.
- `extract_kernel` (`hwcodegen.py:1265`) dispatch generalizes to read a declared `_kernel_method`
  (Phase 2), honoring explicit `control_mode`. Keep the regmap-presence inference as a fallback.

## Phases / sequencing

0. **Elaboration contract** (Phase 0) — `elaborate(class, params)`, sim-free context, param-purity
   determinism check, key-by-param-set. **Prerequisite; do first.**
1. `FreeRunComp` + `SynthComp` + retrofit — **DONE / merged.**
2. **`CompositeComp`** (passive sibling) + **`CodegenSource`** factoring + `SeqTB` off `HwComponent` +
   `extract_kernel` `_kernel_method` dispatch. Retrofit composites (`InterleaverCanon`, `MemCopy`,
   `MemSquare`). Verify composite goldens.
3. **`HostActivated`** — formalize `on_start`/`PER_INVOCATION`; migrate `poly`/`regmap` opportunistically;
   keep the regmap-presence inference fallback.
4. **Deferred:** `ThreadTB`/`SystemCTestbench` + BFM generation (from the boundary spec); `LoopComp`
   (+ new `ControlMode`); invert `SynthComp` to a capability when SystemC-gen lands.

## Docs updates (do alongside the phases)

- `components/taxonomy.md` — the three orthogonal axes + capability rings; add **Composite** as a
  synthesizable kind; state that verification is *inferred*, not class-determined; `run_proc` trichotomy.
- `comp_codegen/structure.md` — the declared-vs-inferred mode note (done for `FreeRunComp`); extend for
  `CompositeComp`/`SeqTB` when they land.
- `overview/targets.md` — the "class does not determine verification" cross-link.

## Open questions

- [ ] `CodegenSource` name / exactly what it carries (`cpp_kernel_name`, `HwParam` machinery,
      `param_supports`) vs. what stays `HwComponent`-only (endpoints, `run_proc`, `control_mode`).
- [ ] `HostActivated` name (vs `HostActivatedComp` / `LaunchedComp`).
- [ ] Does the sim-free elaboration context need a real-ish env for `__post_init__` code that touches
      `sim`, or can structural declaration be fully decoupled from sim wiring?
- [ ] Structural-equality notion for the determinism check (compare endpoints + sub-comp graph +
      interface bindings; ignore names/identity).

> Note: untracked plan; touches no tracked files. Grounded in hwcodegen.py:777/1265/745,
> hwcodegen_steps.py:75, hwgen.py:1195-1198/1443/1971, hw_component.py:160/251, simobj.py:87,
> simulation.py:45, hw_testbench.py:26, examples/stream_inband/poly.py:335.
