# Plan: component class hierarchy (execution model × structure) + the elaboration contract

## Status

- **Phase 1 DONE (merged):** `SynthComp` + `FreeRunComp` (`run_iter`), retrofit of `MemRStream`/`MemWStream`
  + the six interleaver tiles, docs. Goldens byte-identical; construction-time synthesizability check works.
- **Phase 0 DONE (merged, `7430d5d`):** the elaboration contract — `waveflow/build/elaborate.py`:
  `elaborate(class, params)` is the single codegen instantiation entry; a sim-free `ElabContext`; a
  param-purity determinism gate keyed per `(class, param-set)`. Byte-identical output; caught + fixed a
  real latent bug (`IlElem` shared-class mutation).
- **Phase 2 A+B DONE (merged, `302132b`):** `CompositeComp` (passive bodyless sibling; `__init_subclass__`
  rejects `run_iter`) + `select_kernel_method` (one source of truth for the kernel entry). Byte-identical;
  baseline-green.
- **Phase 2 C — design-options report** (`plans/codegen_source_options.md`): the clean `SeqTB`-off-`SimObj`
  split is option (c), a hierarchy-wide refactor; **deferred to a dedicated "C-real" PR** (below); the
  cosmetic Option B deliberately not shipped.
- **Phase 3 DONE (merged, `9379046`):** `HostActivated` (regmap-launched leaf, `_kernel_method='on_start'`)
  + the `_kernel_method`/regmap **trap fix** (`SynthComp._kernel_method` default `'run_proc'` → `None`).
  Migrated `simp_fun` + `poly` byte-identical; `hist`/`vmac` correctly stay `HwComponent` (stream-controlled,
  future `LoopComp`).
- **Phase 4 DONE (merged, `c2f13e1`):** typed codegen dispatch — `waveflow/build/codegen_dispatch.py`
  `codegen_path(comp)` (an `isinstance` ladder) replaces `select_kernel_method`, which is **removed**.
  `CodegenPath.kind` (leaf/composite/testbench) is the future `(class × target)` seam; the
  `_kernel_method`/regmap trap is now structurally impossible. Byte-identical everywhere.
- **Next (candidates, all independent, none urgent):** `run_once` invocation method (below); C-real; the
  deferred `LoopComp`/SystemC-gen work (which is what makes Phase 4's `kind × target` axis load-bearing).

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

0. **Elaboration contract** — `elaborate(class, params)`, sim-free context, param-purity gate. **DONE / merged.**
1. `FreeRunComp` + `SynthComp` + retrofit — **DONE / merged.**
2. **A — `CompositeComp`** (passive sibling; retrofit `InterleaverCanon`/`MemCopy`) + **B —
   `select_kernel_method`** (kernel-entry unification) — **DONE / merged.** **C — `CodegenSource`/`SeqTB`**
   is a design-options report (`codegen_source_options.md`), deferred to "C-real".
3. **`HostActivated`** + the `_kernel_method` trap fix — **DONE / merged.**
4. **Typed codegen dispatch** — `codegen_dispatch.codegen_path`; `select_kernel_method` removed. **DONE / merged.**
5. **`run_once` invocation method** (see section below) — a host-activated component's Python invocation
   that mirrors the C++ function call; makes the sequential-testbench codegen a 1:1 lowering. Additive;
   testbench-side; independent.
6. **C-real** — the `CodegenSource`/`SeqTB` refactor per `codegen_source_options.md` (two byte-identical
   checkpoints). Not urgent; testbench-side; independent.
7. **Deferred:** `ThreadTB`/`SystemCTestbench` + BFM generation; `LoopComp` (+ new `ControlMode`); invert
   `SynthComp` to a capability when SystemC-gen lands.

## `run_once` invocation method (Phase 5) ✅ DONE

> **Status (merged, `7a8f463`):** both sub-phases landed. **5a** — `run_once(*args)` on `HostActivated`
> (endpoint-derived inputs/outputs, reuses `on_start`, `__call__` alias, `on_start` now `@abstractmethod`;
> scope = regmap-scalar, streams/yielding raise a clear follow-on). **5b** — the TB extractor lowers
> `dut.run_once(dut.regmap.get(...), ...)` to the *same* canonical kernel call as `dut.run()`, mapped by
> field **name/access** (interleaved-output test proves it), `simp_fun_tb.cpp` byte-identical (independently
> diffed). Additive; 15-failure baseline held; ruff+mypy clean.
>
> **What 5b delivers vs. the payoff:** 5b lands the *extraction seam* (recognition + by-name safety +
> interleaving correctness) — but the TB `run_once` form is still *more verbose than `dut.run()`* for
> identical output. The ergonomic win is the two documented **follow-ons**: **(i) value/local args** —
> `run_once(x, a, b)` emitting the input-field assignments (replaces the `read_uint32_file` lines);
> **(ii) return capture** — `z = dut.run_once(...)` allocating output locals + reading them back (replaces
> `write_status_json`). Together they make a TB read `z = dut.run_once(x, a, b)`. Both cleanly rejected at
> extraction today with clear messages. Stream-bearing `run_once` (push/pop marshalling) is a third
> follow-on. 5a already gives the ergonomic call *in pysim*.

A host-activated kernel's C++ realization **is a function** — `simp_fun(x, a, b, y);` — with the
`ap_start`/`ap_done` handshake and `s_axilite` register I/O generated by the interface pragmas (under the
hood). So the invocation is *one call*. Give the Python side the same shape:

```python
z = dut.run_once(x, a, b)      # mirrors the C++ call, vs. today's
# dut.regmap.set('x', x); …; dut.run(); z = dut.regmap.get('z')
```

- **Why (codegen, not just ergonomics):** the sequential testbench's C++ is a single call; `run_once`
  lowers **1:1** to it, whereas the explicit `regmap.set/run/get` pattern forces the testbench extractor
  to *pattern-match and reconstruct* the call. `run_once` makes the testbench codegen simpler and more
  robust — and the Python testbench then has the *same shape* as the generated C++ testbench (SSOT).
- **Where it belongs:** the **invocation** execution model — `HostActivated` (and a host-activated
  *composite*, i.e. one with a regmap boundary), keyed by `control_mode = PER_INVOCATION` / `ap_ctrl_hs`.
  A free-running kernel never "runs once and returns," so it gets no `run_once`.
- **Signature = the kernel signature**, both derived from the endpoints (RW regmap fields + input streams
  → inputs; R fields + output streams → outputs) — the same source that generates the C++ function
  signature, so they cannot drift. Pure-scalar (`simp_fun`) is the clean case; stream-bearing (`poly`)
  is richer (`run_once` push/pops the streams).
- **Shape:** a **generic framework method** on `HostActivated` that introspects the endpoints (with an
  optional per-component override for a nicer arg spelling). Name: `run_once` (discoverable) or
  `__call__` (`z = dut(x, a, b)` — more literal).
- **Sequencing:** additive / non-breaking (existing testbenches keep the explicit form; migrate
  opportunistically). Two independent steps: **(5a)** the **pysim** `run_once` (endpoint-derived; a small,
  self-contained win); **(5b)** the testbench-extractor **`run_once`→C++-call lowering** (pairs with the
  `SeqTB`/C-real testbench-codegen work). Do 5a first.

## Typed codegen dispatch (Phase 4)

Replace the generic body-selector (`select_kernel_method` + `_kernel_method`) with **dispatch by
component class**, over the *shared* extraction engine (`HwStmtExtractor(comp, method_name=…)` and
`composite_top_spec`). Each kind's codegen knows its own `method + emitter + pragma`:

| Component class | Extracts | Output |
|---|---|---|
| `HostActivated` | `on_start` | ap_ctrl_hs kernel (s_axilite) |
| `FreeRunComp` | `run_iter` | `ap_ctrl_none` `hls::task` body |
| plain free-running `HwComponent` | `run_proc` | (interim, un-migrated leaves) |
| `CompositeComp` | — (the graph) | composite top (`composite_top_spec`) |
| `SeqTB` / `HwTestbench` | `main` | `<kernel>_tb.cpp` |

- **Shared engine underneath** — the typed steps are *thin*: they supply the method/emitter/pragma; they
  do NOT each re-implement extraction. DRY preserved; only the generic *resolver* is retired.
- **Sets up the target axis** — this is the codegen-side of the realization matrix
  (`overview/targets.md`): a component realizes to *multiple* targets (`hls::task` **and** SystemC
  `SC_THREAD`). A single `_kernel_method` string can't express "which output"; a `(class × target)`
  dispatch can. That is the decisive justification, and it is **future** (no SystemC-gen yet).
- **Discipline:** byte-identical across every kernel + `*_tb.cpp` + composite top; suite at baseline. The
  current dispatch is already partly typed (`is_testbench` → testbench path; composites →
  `composite_top_spec`); this unifies the leaf path (`on_start`/`run_iter`/`run_proc`) into the same
  class-typed structure.
- **Honest status:** with the trap fixed (Phase 3), the *immediate* win is clarity + an explicit BuildDAG,
  not correctness. Deferrable until a multi-target consumer makes it load-bearing — but it removes the
  generic resolver, so doing it before more codegen piles onto `select_kernel_method` is defensible.

## Docs updates (do alongside the phases)

- `components/taxonomy.md` — the three orthogonal axes + capability rings; add **Composite** as a
  synthesizable kind; state that verification is *inferred*, not class-determined; `run_proc` trichotomy.
- `comp_codegen/structure.md` — the declared-vs-inferred mode note (done for `FreeRunComp`); extend for
  `CompositeComp`/`SeqTB` when they land.
- `overview/targets.md` — the "class does not determine verification" cross-link.

## Open questions

- [ ] **The `_kernel_method`/regmap trap (fix in Phase 3).** `SynthComp._kernel_method` defaults to
      `'run_proc'`, and `select_kernel_method` (hwresolve.py) gives an explicit `_kernel_method` **priority
      over the regmap check**. So a regmap-bearing `SynthComp` that relies on the default resolves to
      `run_proc` instead of `on_start`. Dormant today (only `SynthComp` subclass is `FreeRunComp`, which
      overrides to `run_iter` and is never extracted). Fix options: `HostActivated` declares
      `_kernel_method='on_start'`; and/or default `_kernel_method` to `None` and let the regmap fallback
      fire — but that collides with `SynthComp._check_synthesizable` (`getattr(cls, self._kernel_method)`),
      and `select_kernel_method` lives in `build/` so `hw/` can't call it (layering). Resolve carefully.
- [ ] `CodegenSource`: covered by `plans/codegen_source_options.md` (Option C / "C-real").
- [ ] `HostActivated` name (vs `HostActivatedComp` / `LaunchedComp`).
- [ ] The two Phase-0 review notes (defer): purity signature is a *multiset* (order/name-agnostic) — could
      tighten to ordered to catch order impurities; `ElabContext` skips `super().__init__` (safe only while
      `Simulation.__init__` stays trivial — a defensive comment/replication would future-proof).
- [ ] Structural-equality notion for the determinism check (compare endpoints + sub-comp graph +
      interface bindings; ignore names/identity).

> Note: untracked plan; touches no tracked files. Grounded in hwcodegen.py:777/1265/745,
> hwcodegen_steps.py:75, hwgen.py:1195-1198/1443/1971, hw_component.py:160/251, simobj.py:87,
> simulation.py:45, hw_testbench.py:26, examples/stream_inband/poly.py:335.
