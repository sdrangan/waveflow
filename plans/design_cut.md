# Plan: the design cut — one `HwModule`, per-build realization

**Status:** designed 2026-08-11; implementation in progress on branch `design-cut`. Split out of `plans/adc_model.md` (point 1), where it
surfaced as "what kind is the RFDC emulator?". It is not an RFDC question — it is a codegen-architecture
question that the RFDC merely made unavoidable. Resolve this first; `adc_model.md` then reduces to
"three plain `HwModule`s" and can proceed.

Supersedes the open question **"What *kind* is a TB participant?"** in `plans/xsi_tb_codegen.md`
(§Open questions), which proposed "a new kind with `potential_targets = {xsi_bfm_model}`". That is half
right: `xsi_bfm_model` is a real *target*; it is not a *kind*. See "Two rejected answers" below.

## Motivation

A module can be realized in more than one way, and **which realization applies is a property of the
build, not of the class.** Consider a design of three modules. In one synthesis the DUT is `mod1` alone
and `mod2`/`mod3` are XSI testbench models; in another all three are inside the DUT. Nothing about
`mod2` changed — only where the boundary was drawn.

Today the framework half-believes this. The graph half is already cut-driven; the artifact half is not,
and the "is this a DUT or a testbench participant?" question is answered by two `hasattr` probes.

### Two rejected answers

- **A new class** (`ExtMod`, "a module realized outside the boundary"). Rejected: it freezes a
  per-build role into a class fact, and would fight `derive_boundary`, which already computes the role
  from the graph.
- **"Hand-written C++ means it's a different kind."** Rejected by counter-example: `MemRStream` lives
  *inside* the synthesized kernel and its C++ is entirely hand-written (`kernel_task()` names
  `mem_r_stream_task.h`). Extracted-vs-pre-written is an orthogonal axis that already exists
  (`comp_codegen` vs `custom_hooks`) and is not the axis in question.

## The finding: the cut is already free in the graph, and not free in the artifact

Four pieces of evidence, all in the tree today:

1. **The boundary is derived, not declared.** *"a child endpoint not bound to one of the composite's
   internal interfaces **is** a boundary port"* (`hw_freerun.py::boundary`). Only port *names* are
   declared, and only because local names collide. No module states which side it is on.

2. **One module, two cuts, both green.** `MemRStream` is generated as its own top
   (`examples/interleaver/gen/mem_r_stream.cpp`, XSI gate **158**) *and* instantiated as a task inside
   `mem_copy` (`examples/mem_copy/gen/mem_copy.cpp`, gate **2835**). The capability is real and proven.

3. **But the crossing encoding is baked into the hand-written body.** In the same two files:

   | | standalone top | inside `mem_copy` |
   |---|---|---|
   | the `m_out` edge | `hls::stream<ap_uint<64> >` + AXIS pragma | `hls::stream<framed_word<64> >` channel |
   | the body named by `kernel_task()` | `mem_r_stream_task<64>` | `mem_r_stream_framed_task<64>` |

   Two cuts are served by **two separate hand-written artifacts**, chosen by the example author. So a
   module cannot today be moved across the cut without swapping its `kernel_task()`. *This is the thing
   the feature has to buy.*

4. **Roles are decided by `hasattr`.** DUT = `hasattr(c, "boundary")` (`composite_gen.py:833`);
   participant = `hasattr(c, "bfm_model")` (`composite_gen.py:886`). Participants are bare `SimObj`s
   (`StreamDriver`, `StreamSink`, `MemoryMod`), so they have no endpoint registry, and
   `BfmModel.ports` must re-state endpoint attribute names as **strings** matched by `id()`. A rename
   is a runtime `KeyError`, not a type error.

## The model

**One `HwModule`. Two optional realization hooks, exactly symmetric:**

| hook | declares | realization | used when the module is |
|---|---|---|---|
| `kernel_task()` | "my pre-written `hls::task` body is *X*" | inside the top | **inside** the cut |
| `bfm_model()` | "my pre-written cycle model is *Y*" | an `XsiSimObj` beside the top | **outside** the cut |

Both say *"here is my pre-written artifact"* — declared, not extracted. `kernel_task()` is already a
documented optional hook; `bfm_model()` is the same shape, currently duck-typed. A module may declare
either, both, or neither, and **the cut decides which is consulted.**

Consequences:

- **`potential_targets` keeps only the genuine class fact** — how the body is *invoked*
  (control-driven vs. free-running). No cut changes that. It stops being read as DUT-vs-TB.
- **`check(mod, target)` answers per-module lowerability**, derived wherever possible.
- **A module with neither hook is a pysim-only node** (an RF channel, a golden reference). That is a
  *finding* from `check`, not a declaration — nothing has to say "I have no realization".

### The asymmetry to keep honest

The two checks are not the same animal, and the plan must not imply they are:

- **`composite_kernel` is derived.** Gate 4 runs the real extractor and converts a raise into a verdict
  — no second copy of the rules (`codegen_check.py`). This is why `check(StreamDriver,
  "composite_kernel")` can answer **no** with a real reason, without anyone declaring it.
- **`xsi_bfm_model` is resolved.** A registry lookup plus port coverage. It can only ever answer "you
  declared a model and its ports line up" — never "your Python behavior is realizable as a BFM".

Making them symmetric needs a second extractor backend lowering the statement vocabulary onto the
`sample`/`update`/`drive` cycle protocol. Real, large, explicitly **not** in this plan (see §Not in
scope).

## The predicate: what XSI-lowerability actually requires

Of a *(module, cut)* pair:

1. **Only cut-crossing endpoints count.** Endpoints internal to the module's own subtree are
   irrelevant.
2. **Every crossing endpoint has a *dual* BFM** — the module presents the opposite role of the DUT
   port it faces. Today's registry is two entries (`_SLAVE_FOR_KIND = {maxi_read: AxiMmReadSlave,
   maxi_write: AxiMmWriteSlave}`) plus AXIS taking the participant's declared class. **AXI-Lite has no
   model**, so a regmap / `HostActivated` DUT cannot be XSI-lowered at all today.
3. **A resolvable model**: `bfm_model()` names a class in `waveflow/build/xsi/` implementing
   `XsiSimObj`'s five phases, whose `ports` cover **every** crossing endpoint. Uncovered ports already
   fail loudly (`composite_gen.py:912`).
4. **Config is reachable**: everything the behavior needs that varies is a `DynParam` with a
   `_render_dyn_value`-able value *and* a matching C++ member. Emitted blind today as
   `<model>.<field> = v;` — a missing member is a compile error; a member that means something subtly
   different is silent.
5. **Behavioral equivalence** between the Python body and the C++ model. Nothing checks this and
   nothing statically can. The standing answer is a byte-identical vector gate.

Items 1–4 are mechanically checkable. Item 5 stays a discipline, and should be *stated* in the docs
rather than implied away by the existence of a `check`.

## Stages

Each stage is independently landable and independently gated. The four XSI cycle gates
(**158 / 176 / 2835 / 3469**) are the safety net throughout: this is a re-typing and re-siting
exercise over code that works, so any drift is a regression, not a design change.

### S0 — a verdict exception for the graph walks *(prerequisite)* — **DONE**

`check` gate 4 converts `SynthesisError` into a verdict; the two *graph* paths
(`composite_top_spec` / `tb_top_spec`) raise `ValueError`/`TypeError`, which propagate as bugs. So
`check(StreamDriver, "composite_kernel")` today raises `AttributeError` (no `kernel_task`) instead of
answering **False**. Every per-module question in this plan needs that fixed first.

Already flagged as a deliberate follow-up in `codegen_check.py::_check_generates` and
`plans/one_component_two_flows.md`. Give the graph walks a verdict exception and classify their
"this will not lower" raises.

**Gate:** `check(StreamDriver, "composite_kernel") == (False, <reason naming the missing hook>)`;
every existing `check` test unchanged.

**Landed as.** `LoweringError(SynthesisError, ValueError, TypeError)` in `build/hwcodegen.py`, beside
`SynthesisError`. Every "this graph will not lower" raise in `composite_gen.py` (16 sites, both
walks) and the four structural raises in `hw_freerun.py` are classified as it; gate 4's existing
`except SynthesisError` converts it for free. `composite_top_spec`'s blind `sub.kernel_task()` became
`_kernel_task_of`, which raises a `LoweringError` **naming the missing hook** instead of a bare
`AttributeError`.

Two findings worth recording, because the plan's text assumed otherwise:

- **`check(StreamDriver, "composite_kernel")` never raised `AttributeError`.** It answered `False` at
  *gate 2* (an empty `potential_targets`), which is a correct verdict with an unhelpful reason: it
  named the missing *kind*, not the missing *hook*. So the gate was met by making the refusal
  hook-aware rather than by reaching gate 4 — `REALIZATION_HOOKS` (`codegen_targets.py`) maps a target
  to the hook that realizes it, and `_hook_clause` appends "It also declares no `kernel_task()` hook".
  That table is a **message aid, not a rule** — no verdict is computed from it. S3 adds its second row.
- **The exception contract was not changed under existing callers**, which the plan flagged as the
  stop-and-report risk. `LoweringError` inherits `ValueError` *and* `TypeError`, so all six existing
  `pytest.raises(ValueError/TypeError)` sites around the graph walks pass untouched. The
  classification is purely additive; no caller had to be audited for which of the two a given raise
  happened to use.

`TopSpec.trace_manifest`'s duplicate-instance `ValueError` was deliberately **left unclassified**: it
is downstream of lowering (the spec already built) and gate 4 never reaches it.

**Result:** `tests/build/test_codegen_check.py` 60 passed (4 new); dev loop unchanged at the 6
pre-existing failures; `-m xsi` unchanged (13 passed, 1 pre-existing `fir_block` failure — verified
byte-identical against a stashed clean tree, see the note under Verification).

### S1 — `bfm_model()` becomes a documented hook; participants become `HwModule`s — **DONE**

Promote `bfm_model()` from duck-typed convention to an optional hook on `HwModule`, documented as the
peer of `kernel_task()`. Migrate `StreamDriver`, `StreamSink`, `MemoryMod` from `SimObj` to
`HwModule` and register their endpoints with `add_endpoint`. `HwModule` **is** a `SimObj`, so
`pre_sim`/`run_proc` are untouched. `add_comp`'s type annotation becomes true. Replace the
`hasattr(c, "bfm_model")` probe with the hook.

**Gate:** all four XSI gates byte-identical; `tests/build/test_tb_top_spec.py` green.

**Landed as.** `HwModule.bfm_model()` is a base method that **raises** — overriding it *is* the
declaration. `StreamDriver` / `StreamSink` / `MemoryMod` are `HwModule`s and register their endpoints
(`MemoryMod` registers `m_mm` as well as `s_mm`: a curated subset is the kind of thing that goes
stale).

Three consequences the plan did not anticipate, each of which is the interesting part:

- **`hasattr` stops being a usable probe, and that is the point.** Once the hook exists on the base,
  `hasattr(c, "bfm_model")` is `True` for *every* module including the DUT — so `tb_top_spec`'s
  participant probe would have swept the DUT in with the participants. `declares_hook(source, hook)`
  (`hw_module.py`) compares against the base by identity, the way `FreeRunMod._kind` detects a
  `run_iter` override. A canary test pins this, because the failure mode is a silently wrong
  participant set rather than an error.
- **`MemoryMod` becoming an `HwModule` broke the *sequential* extractor**, nowhere near the XSI path.
  The TB `main()` walk dispatched `_try_dut_bind` (accepts any `HwModule`) before `_try_mem_bind`, so
  a memory was claimed as a DUT and then rejected for having non-literal kwargs — 8 failures across
  `hist` and `block_scale`. Fixed by probing **most-specific-first** (memory before DUT), which is a
  rule rather than a negative condition someone has to maintain. Worth noting as a general hazard:
  widening a base class re-orders every `issubclass` dispatch that was previously disjoint by accident.
- **Open question 4 is resolved.** `BfmModel.ports` stay **attribute names** — constructor order is a
  fact about the C++ and nothing else records it — but `_bfm_port_endpoint` now validates each against
  the module's `add_endpoint` registry, so a stale entry is an elaboration-time `LoweringError` naming
  both namespaces instead of a runtime `AttributeError` (or, worse, a silent resolution to some other
  attribute that models the wrong port).

**Result:** `-m xsi` byte-identical — 13 passed / 1 pre-existing `fir_block` failure, and **no
generated artifact changed** (the XSI runs regenerate the harnesses in place; `git status` showed only
source edits). Dev loop back at 6. `tests/build/test_tb_top_spec.py` 9 passed (4 new).

### S2 — an explicit protocol × role → BFM registry — **DONE**

Lift `_SLAVE_FOR_KIND` into a complete, explicit table keyed by `(endpoint kind, role)`, with the
AXIS entries included rather than implicit in the participant's declared class. A missing entry
becomes a named gap, not a `KeyError`. Record AXI-Lite as the known hole (S6).

**Gate:** the registry reproduces today's model selection for all four designs; a synthetic
unregistered kind produces a message naming the protocol and the role.

**Landed as.** `BfmDual(protocol, role, model, participant_declares)` + `BFM_DUALS` keyed by
`kind_of_endpoint`'s vocabulary, and one `bfm_dual_class(kind, declared)` that every caller goes
through. `protocol` and `role` are spelled out as separate fields precisely so a missing dual reports
*"no BFM implements the master role of AXI4-Lite"* rather than a `KeyError` on a kind string.

- **`participant_declares` is the table's one asymmetry**, and it is real rather than a compatibility
  shim: on AXI-Stream the role fixes the direction but not the class (a source, a sink, and a peer
  that never backpressures are three classes in one role), while on `m_axi` there is nothing to
  choose — the DUT is the master, so the DUT's port kind picks the slave and the participant supplies
  only the arena.
- **AXI-Lite is a row, not prose.** `kind_of_endpoint` now returns `axilite_slave` for a
  `RegMapMMIFSlave` — previously it raised "no boundary kind for endpoint type", which is the wrong
  diagnosis. `BFM_DUALS["axilite_slave"].model is None` makes the hole part of the one lookup that
  answers "which duals exist". `_boundary_port` still refuses to lower it, which is correct.
- **One unreachable behaviour was tightened.** Previously a *non-shared* participant on an `m_axi`
  port would have taken its own declared class; now the registry's class wins regardless of sharing.
  Nothing does this today (only shared `MemoryMod`s sit on `m_axi` ports), and the new behaviour is
  the one the design intends: a memory does not get to decide whether it is read or written.

The reproduction gate is asserted against the **committed generated harnesses** (`*_tb_harness.h`) —
the artifacts that were actually compiled and run through RTL — not against a restatement of the
table, which would only prove the table equals itself.

**Result:** `tests/build/test_tb_top_spec.py` 16 passed, 1 skipped (`InterleaverInbandTB` has no
committed harness to compare against). Dev loop at 6. `-m xsi` unchanged, no artifact drift.

### S3 — the target `xsi_bfm_model` + its gate

Add `XSI_BFM_MODEL = "xsi_bfm_model"` to `codegen_targets.py` (per-**module**, distinct from
`sequential_xsi_tb`, which is per-**graph**). Gate 4 implements predicate items 1–4: resolve
`bfm_model()`, check the class exists, check `ports` covers the crossing endpoints, check each has a
dual in the S2 registry, check `DynParam`s render.

Default cut = **all** endpoints (the strictest, cut-independent answer); optional `crossing=` argument
narrows it.

**Gate:** `check(StreamDriver, "xsi_bfm_model")` True; `check(MemCopy, "xsi_bfm_model")` False with a
message naming the uncovered port; `check(<a module with an AXI-Lite port>, "xsi_bfm_model")` False
naming the missing dual.

### S4 — the cut becomes an argument

`_find_dut` discovers the DUT by probing for `boundary`; make the DUT an explicit parameter
(`tb_top_spec(tb, dut=...)`) with today's discovery as the default. This is what lets one graph
produce two cuts.

**Gate:** all four designs regenerate byte-identically with the DUT named explicitly.

### S5 — the crossing encoding stops being baked into the body ⟵ *the stage that buys the feature*

S1–S4 are hygiene and checkability. **This** is the finding from evidence item 3: `mem_r_stream_task`
vs `mem_r_stream_framed_task` are two hand-written artifacts for one module at two cuts. Until the
framing shift (internal `framed_word<W>` ⟷ boundary AXIS + `TLAST`) is derivable, "move the cut" still
means "write another body".

Two candidate shapes, to be decided against the witness rather than on paper:
- the framing becomes a **template parameter** of one body, or
- the framing conversion becomes an **adapter task** the generator inserts at the boundary.

Also in scope here: `StreamIF.depth` is a physical property that must survive the move
(`reference-fifo-depth-is-physical`).

**Gate:** `mem_r_stream` generated at **both** cuts from **one** module declaration — standalone top
(158) and inside `mem_copy` (2835) — with no second hand-written body.

### S6 — the docs

**Mostly deltas, not new pages.** Four of the five pages this feature needs already exist, and two of
them already name `bfm_model` — so the risk is *drift*, not absence. `docs/guide/comp_codegen/xsi_tb.md`
carries `bfm_model` in its `api:` frontmatter today while the code treats it as duck-typed convention;
after S1 the page becomes true without changing a word of its claim.

| page | status | what changes |
|---|---|---|
| `guide/flows/modules.md` | edit | The taxonomy page — today it sorts modules into kinds ("a plain `HwModule` is a simulation-only model; `HostActivated` → sequential; `FreeRunMod` → concurrent"). It gains the **second axis**: *kind* = how the body is invoked (class fact); *hooks* = how it is realized (`kernel_task()` / `bfm_model()`); *cut* = which hook applies (build choice). This is where "the cut is a build choice, not a class fact" belongs, because it is the foundation page both flows read. |
| `guide/flows/concurrent.md` | edit | Make the **DUT/TB boundary** an explicit concept rather than an assumption: the boundary is *derived* (an endpoint not bound to an internal interface **is** a boundary port); an internal channel and a boundary port are **different objects** (`framed_word<W>` vs AXIS + `TLAST`); re-cutting is a build choice with a cost. Carries the `mem_r_stream` two-cut worked example — real, already gated (158 standalone / 2835 inside `mem_copy`). |
| `guide/comp_codegen/xsi_tb.md` | edit | `bfm_model()` is a **documented hook**, not a duck-typed convention (S1). Add `xsi_bfm_model` (per-**module**) alongside `sequential_xsi_tb` (per-**graph**) and say plainly which question each answers. State the **resolved-vs-derived asymmetry**: what `check` can and cannot tell you about a BFM. |
| `guide/build/bfm.md` | edit | Already documents the model library and the `XsiSimObj` lifecycle. Add the S2 **protocol × role registry** as a table, so "which duals exist" has one lookup, and name the AXI-Lite hole in the same table rather than in prose. |
| `guide/custom_hooks/bfm_model.md` | **new** | The authoring page. Sited as the sibling of `custom_hooks/writing.md` (how to write a `hls::task` body) — **the symmetry is the pedagogy**: one section teaches both pre-written realizations, inside and outside the cut. |

The new page's contents, in order:

1. **When you need a new BFM — usually you don't.** Reuse `AxisMaster`/`AxisSlave`/`AxiMm*Slave`. A new
   model is warranted only when the peer's *protocol behavior* differs (e.g. a converter that never
   backpressures and must count underruns), not when its *data* differs — that is a bundle.
2. **The five `XsiSimObj` phases**, and why `sample`/`update` are split (a beat is decided from values
   sampled before the edge and applied after it; collapsing them silently changes when a transfer is
   seen).
3. **The config contract** — `DynParam` fields emitted as `<model>.<field> = v;`, the required matching
   C++ member, and the **falsy-value trap** (`discover_dyn_params` skips falsy values, so `0.0`/`False`
   emit nothing and silently take the C++ default).
4. **The conformance obligation** — predicate item 5. Nothing checks that the Python body and the C++
   model agree; the page must say so and require a byte-identical vector gate, not imply that `check`
   covers it.
5. A worked example, from the simplest real model in the library.

**Docs rules that apply here** (existing discipline, not new):

- The target vocabulary must not drift: `codegen_targets.py` already carries a "must not drift from
  `guide/flows/index.md`" contract. `xsi_bfm_model` (S3) lands in the code and the docs **in the same
  commit**.
- Reference flow steps **by name** with a link to the flowsteps page — never a hard-coded "Step N".
- A figure would earn its place here: one diagram, the same three-module graph under two cuts. Use the
  committed-figure workflow (TikZ → SVG via `render.sh`), not an ASCII sketch.

**Gate:** `tests/docs/test_markdown_integrity.py` (the link guard checks every relative target) and
`tests/docs/test_documented_numbers.py` — the latter matters because the cycle counts this plan leans on
appear *in* the docs, so an S5 change to `mem_r_stream`'s generated form must update both or fail.

### S7 — deferred

- **AXI-Lite BFM** (unblocks XSI for `HostActivated` DUTs).
- **An XSI backend for the extractor** — the only thing that would make `xsi_bfm_model` a *derived*
  rather than *resolved* verdict.
- **`bitstream` / IPI** as a third realization hook. The shape this plan establishes is where it
  attaches; do not design for it yet.

## Verification

**Measured baselines (2026-08-11, before any edit on this branch).** Recorded so pre-existing red is
never mistaken for a regression:

- dev loop (`-m "not vitis and not xsi"`): **6 failures** — `test_dataschema_poly` ×1 and
  `poly/test_timing_analysis` ×5. Matches the long-standing recorded baseline.
- `-m xsi`: **14 tests, 13 passed, 1 failed.** The failure is
  `tests/examples/test_fir_block_xsi.py::test_rtl_matches_golden_across_reload_and_carry`
  (`block 0 word 0: 0x00000000 != golden 0x0dab0666` — the RTL dumps a zero arena). It is **not** one
  of the four cycle gates, and it was confirmed pre-existing by stashing this branch's work and
  re-running `-m xsi` against the clean tree: byte-identical failure. The four cycle gates
  (158 / 176 / 2835 / 3469) are green throughout.

- The four XSI gates (158 / 176 / 2835 / 3469) after every stage. Nothing here is allowed to move them.
- New unit tests in `tests/build/test_codegen_check.py` (S0, S3) and `tests/build/test_tb_top_spec.py`
  (S1, S2, S4).
- S5 is the only stage whose gate is a *new* artifact rather than an unchanged one.
- Docs (S6): `tests/docs/test_markdown_integrity.py` + `tests/docs/test_documented_numbers.py`.

## Not in scope

- **Generating BFM C++ from Python.** Same anti-goal as `plans/xsi_tb_codegen.md` Stage 0: a
  hand-written, cycle-exact protocol layer is verified code; re-deriving it is not progress.
- **RFDC / `adc_model.md`.** Returns after this lands.
- **`TbGraph`.** `MemCopyTB` inherits `FreeRunMod` purely for `ordered_subcomps` / `interfaces` /
  `boundary`, then overrides `potential_targets` to say it is not a kernel. An honest home for those
  is a separate refactor and must not ride along.

## Open questions

- Is "all endpoints" the right default cut for `check(mod, "xsi_bfm_model")`, or should the absence of
  a cut make the question ill-posed?
- Two cuts of one module produce two artifacts. What are they called, and how does the DAG keep them
  distinct? (`mem_r_stream` the top vs. `mem_r_stream` the task inside `mem_copy` already coexist —
  by living in different examples. That will not scale.)
- Does the framing shift (S5) belong in the body or in an inserted adapter? Decide from the witness.
- `BfmModel.ports` are attribute-name strings; `add_endpoint` keys by `endpoint.name`. Those two
  namespaces need reconciling once participants have a registry (S1) — worth doing, since it converts
  a runtime `KeyError` into an elaboration-time error.

## Notes carried in

- **csynth OK is not evidence of correctness** (`reference-hls-hook-csynth-gotchas`). Every claim here
  is gated on XSI.
- **pysim and XSI are expected to disagree on timing** (`plans/xsi_tb_codegen.md`) — that is the model,
  not a bug. Nothing in this plan compares them.
- **`XsiSimObj` is C++-only, and that is deliberate.** The class in `waveflow/build/xsi/xsi_bfm.h`
  (landed in `fed661f`, documented in `docs/guide/build/bfm.md`) is live and inherited by all five
  models. There is **no Python `XsiSimObj`** — zero hits across the tree. A Python counterpart (a *kind*
  for testbench participants) was proposed and never built, and this plan is why it stays unbuilt: the
  answer is a hook on `HwModule`, not a class. Do not re-propose it.
- The duck-typed walk was a *deliberate* earlier choice ("prototype the walk with duck-typing FIRST;
  decide the participant kind after, once the walk has shown what it actually needs"). It has now shown
  what it needs; S1 is the follow-through, not a reversal.
