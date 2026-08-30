# Plan: behavioral edges — an `Interface` that carries behavior, in both backends

**Status:** designed 2026-08-12, not started. Split out of the ADC/RFDC design discussion, where
`RFSampIF` (a block-rate sample channel that owns a metronome, a buffer, and underflow accounting)
turned out to need machinery that does not exist. It is not an RF feature — it is XSI testbench
infrastructure, shared with monitors and scoreboards. `plans/adc_model.md` stage 2 depends on it.

**Promotes a deferred decision.** `plans/xsi_tb_codegen.md` §"Deliberately deferred" rejected
*channel-as-a-class* for want of a motivating case, and named its own re-opening condition:

> It **earns itself** the moment an edge needs *behaviour*: instrumentation (log/count/inject
> backpressure), or a **model→model channel** (a monitor feeding a scoreboard) where there is no RTL
> between and a real queue must exist.

`RFSampIF` hits **both** clauses at once. This plan is that item, promoted. The same note records that
it is *"cheap to reverse — purely emitter output shape; the Python graph is identical either way,"*
which bounds the risk.

## The invariant this exists to protect

From `plans/xsi_tb_codegen.md`, stated twice and load-bearing:

> *"If those were participants, the pysim graph and the XSI graph would have different nodes and 'one
> statement, two backends' breaks on the first example."*

A design where a module has four endpoints in pysim and two in XSI violates it. The rule that resolves
this is already written in `plans/design_cut.md` (predicate item 1): **only cut-crossing endpoints need
a BFM dual.** An endpoint whose peer is also outside the cut needs *no dual* — which is a statement
about duals, not about existence. The endpoint still exists; its peer still exists; the edge between
them is simply realized as a model↔model channel rather than as RTL pins.

Today that realization has no emitter, so the temptation is to collapse the peer into a file read by
the neighbouring model — and that is the invariant violation. This plan removes the temptation.

**Scope note.** The invariant says *a given testbench graph has the same nodes in both backends*. It
does **not** say every graph runs in both. A graph containing a pysim-only node (a channel model doing
numpy DSP) simply answers `False` to `check(tb, "sequential_xsi_tb")` and fails at generate time. That
is correct behavior, not a gap.

## What exists today

- `tb_top_spec` walks **`dut.boundary`** and emits one BFM per DUT port. An edge with no DUT port on
  either end emits nothing — it is not rejected, it is *invisible*.
- `BFM_DUALS` is keyed by the **DUT's port kind**, because the DUT boundary is the spine of that walk.
  A model↔model edge has no DUT port kind, so the table cannot answer for it.
- `Interface` is already a `SimObj` (`interface.py:231`), so it already has `pre_sim` / `run_proc` /
  `post_sim` in pysim. The Python half of "an edge with behavior" needs nothing new.
- `StreamIF.depth` is already *"a physical property, single-source for both backends"* — an edge owning
  a hardware property, read by pysim as a queue bound and emitted as `#pragma HLS STREAM depth=N`. The
  precedent for edge-owned state is established; only its **XSI** realization is missing.

So the gap is precisely: **an `Interface` can carry behavior in pysim and cannot carry it in XSI.**

## The model

An `Interface` that declares `xsi_model()` — the edge-side twin of a module's `bfm_model()` — lowers to
a C++ `XsiSimObj` that owns a **queue** and is bound by *both* peer models, with no RTL between.

| | module (node) | interface (edge) |
|---|---|---|
| pysim | `run_proc` on a `HwModule` | `run_proc` on an `Interface` — *exists today* |
| XSI | `bfm_model()` → an `XsiSimObj` bound to RTL pins | **`xsi_model()` → an `XsiSimObj` bound to two peers** |

**Why a queue and not a direct call.** The five-phase protocol already supplies the discipline: a
producer writes in `update()`, a consumer reads in the next cycle's `sample()`. That makes the transfer
deterministic **regardless of participant list order**, which a direct call is not. This is what the
deferred note meant by *"a real queue must exist"*.

**Rate conversion belongs to the edge.** A behavioral edge typically runs on its own clock (a sample
rate), while the XSI harness steps on the AXI clock. The conversion is a fractional-credit accumulator
owned by the edge's model:

```cpp
credit += rate_ratio;                 // fractional, derived: f_edge / f_axis
if (credit >= 1.0) { credit -= 1.0; /* one edge-tick this cycle */ }
```

Derived, never declared — the same shape as the AXIS-side accumulator in `adc_model.md`. Two
granularities, one mechanism.

## Stages

**S1–S3 DONE 2026-08-12** (branch `behavioral-edges`, 3 commits). S4 and S5 untouched.

### S1 — `xsi_model()` as an `Interface` hook — **DONE**

Declare the hook and its resolver, mirroring `bfm_model()`: `declares_hook(iface, "xsi_model")`, a
`ChannelModel` record (C++ class, the two peer endpoints in constructor order, `extra_args`), and
`DynParam` config emission. No emitter changes yet.

**Gate:** unit tests only; the four XSI designs unchanged (none declares the hook). — *met.*

**Deviation: `ChannelModel.peers` names the interface's SIDE names, not endpoint attribute names.**
`BfmModel.ports` has to be attribute names because C++ constructor order is recorded nowhere else and
`_bfm_port_endpoint` must reconcile two namespaces. An interface *owns* its sides (`endpoints` is
keyed by them), so that problem simply does not arise and the record is the shorter one.

**A required change nobody costed: `declares_hook` had to learn its sentinel base.** It compared
against `HwModule`, and `getattr(HwModule, "xsi_model")` is `None` — so `fn is not None` would have
answered **True for every interface including the base**. That is the exact `hasattr` trap the
predicate exists to close, one level up, and it would have made the second walk treat every `StreamIF`
in every design as a behavioral edge. Fixed with `_hook_sentinel_base` (Interface for an interface,
HwModule otherwise) plus an explicit `base=` override; the module side is byte-for-byte unaffected,
including `kernel_task`'s deliberate no-sentinel case, and both are tested.

**Open question resolved (per the plan's own list): an edge model needs no `pre_sim`/`post_sim` file
I/O.** Bundle I/O lives on **nodes** — `RfDataSource.pre_sim` reads `in_bundle`, `RfDataSink.post_sim`
writes `out_bundle`, and `RFSampIF` has no file I/O at all. That is the same split
`StreamDriver`/`StreamSink` versus `StreamIF.depth` already uses, and it is why `BlockChannel` carries
no bundle machinery. A channel that logged its own traffic would be a monitor node attached to it.

### S2 — the C++ channel primitive — **DONE**

A `wfbfm::BlockChannel<T>` in `waveflow/build/xsi/`: a bounded deque with the phase discipline
(`update()` publishes, next `sample()` observes), a depth, and **drop / starve counters** so an overrun
or underrun is a number rather than a silent behavior. Plus the fractional-credit accumulator as a
reusable `RateTick` helper.

**Gate:** a standalone C++ test, driven the way `tests/build/test_xsi_bundle_io.py` drives the bundle
round-trip — no Vivado needed, `g++ -fsyntax-only` plus a run. — *met, and it compiles **and runs**
rather than only type-checking.* `tests/build/test_xsi_channel.py`, 8 tests: deferred visibility,
order-independence in both registration orders, the one-cycle hop, the depth bound counting staged
items, peek-is-not-a-starve, `RateTick` at 256/300 over 300 cycles, and a ratio > 1 aborting.
**Verified sensitive:** making `push()` commit eagerly fails 3 of them, including the
order-independence one.

**Deviation: `XsiSimObj` had to be split out into `xsi_simobj.h`.** It was defined in `xsi_bfm.h`,
which reaches Vivado's `xsi.h` — so a `BlockChannel : public XsiSimObj` could not have been compiled
without the toolchain, and the gate above could not have existed. The split is the honest factoring
anyway: a lifecycle base is not a bus model, and an edge model binds *models* rather than *pins*.
`xsi_bfm.h` includes it, so every existing user is unaffected. Both new headers join
`XsiHarnessStep`'s copy list.

**A trap found on the way, not fixed here.** The committed `examples/*/xsi/xsi_bfm.h` copies are
**already stale** against `waveflow/build/xsi/xsi_bfm.h` (missing a 9-line block added later), and
nothing checks it. The XSI gates compile against those copies, so they are unaffected by the split —
but a freshly generated workspace and a committed one are now running different library code, which
is a silent-drift hazard of exactly the kind `test_committed_rtl_f_matches_the_rtl_on_disk` exists to
catch for the `.f` files. Worth its own gated change.

### S3 — the second walk in `tb_top_spec` — **DONE**

Today's walk iterates the DUT boundary. Add a second pass over the TB's interfaces whose endpoints are
**both** outside the cut, emitting a channel and binding the two peer models to it. Declaration order
must place a channel before both of its peers (the harness already orders shared objects first).

The two walks must not double-count: an interface with one endpoint on the DUT boundary is the existing
case and stays there. An interface with *neither* endpoint on a participant is an error, not a no-op.

**Gate:** all existing designs byte-identical (none has a non-boundary edge); a new minimal two-model
fixture emits a channel, compiles, and moves a value between two participants across a cycle boundary.
— *met, in three pieces.* Byte-identity is checked against the **committed** harnesses using the very
TB factories their generators use (`make_xsi_tb`), plus the three XSI cycle gates unmoved. The
fixture (a monitor → scoreboard token edge, the *other* clause of the deferred note this plan
promotes) emits a channel and its two peers; the emitted harness compiles under `-fsyntax-only`
(Vivado-gated, because it reaches `xsi.h`); and the value-across-a-cycle-boundary claim is the
toolchain-free C++ gate in S2, which is where it can actually be *run*.

**Deviation: a module may not sit on both a boundary port and a behavioral edge.** `bfm_model()`
names one C++ class for the whole module, and the two bindings have different constructor shapes —
`(sim.dut(), ports::X, …)` versus `(channel, …)`. It is **refused with that sentence** rather than
emitted wrongly. This matters for `plans/adc_model.md` stage 2: the `Rfdc` is exactly such a module
(two AXIS endpoints crossing the cut, two RF endpoints not), so stage 2 must first give `BfmModel`
per-port resolution — the generator would then resolve each named port to a DUT prefix *or* a channel
name and pass them in `ports` order to one constructor. That is a `BfmModel` change, not a walk
change, and the walk is written so it stays one.

**Added, not in the plan: a name-collision check.** Every emitted identifier (shared objects,
channels, models) shares one struct scope, so a collision would *shadow* rather than fail to compile
— a model silently binding the wrong object. Checked once over all three sources.

**Also found: a channel's `DynParam` must land on a real C++ member**, exactly as a model's does, and
nothing static checks either side. Caught by the harness compile test, which is the only thing that
would have.

### S4 — the pysim/XSI equivalence gate

The obligation predicate item 5 names, made concrete for edges: **both realizations must agree on the
counters** (items transferred, dropped, starved) for the same scenario. This is the pattern every
behavioral edge inherits, so build it once as a reusable harness rather than per-edge.

**Gate:** a scenario run through both backends produces identical counter tuples.

### S5 — deferred

- **Instrumentation edges** (log / count / inject backpressure) — the *other* clause of the deferred
  note. Free once S1–S3 exist; not designed here.
- **Monitor → scoreboard edges**, the original motivating case.
- Multi-producer / multi-consumer channels. One producer, one consumer until something needs more.

## Docs

| page | status | what it says |
|---|---|---|
*(Written for S1–S3: `guide/custom_hooks/behavioral.md` (mechanism half only — the `RFSampIF` worked
example needs its channel model, which is `adc_model` stage 2), the `guide/build/bfm.md` edit, the
`guide/comp_codegen/xsi_tb.md` edit, and the one-line `guide/interface/index.md` entry the new page
needs to be reachable. `overview.md` and the `custom_hooks/bfm_model.md` cross-reference are not yet
written.)*

| `guide/interface/overview.md` | edit | The axis this adds: an interface is not only a *wiring* record — it may carry **behavior and state** (`StreamIF.depth` already does; a behavioral edge adds a `run_proc`). Name the two hooks side by side so `bfm_model()` (node) and `xsi_model()` (edge) are learned together. |
| `guide/interface/index.md` | edit | One line in the section index pointing at the new page below. |
| `guide/custom_hooks/behavioral.md` | **new** | The authoring page for a behavioral edge: when an edge deserves behavior *(rate conversion, buffering, loss accounting — not signal processing, see below)*; the `run_proc` half; the `xsi_model()` half; the queue phase discipline and why a direct call is wrong; the counter contract; the equivalence obligation and its gate. |
| `guide/build/bfm.md` | edit | The model library page currently implies every model binds RTL pins. Add the channel: models may bind **each other**, `BlockChannel` is the primitive, and the write-in-`update` / read-in-next-`sample` rule is what makes it order-independent. |
| `guide/comp_codegen/xsi_tb.md` | edit | `tb_top_spec` now has **two walks** — the DUT boundary (one model per port) and non-boundary edges (one channel per edge). State which walk claims which interface, and that an interface reaching neither is an error. |
| `guide/custom_hooks/bfm_model.md` | edit | A cross-reference: this page teaches node models; `guide/custom_hooks/behavioral.md` teaches edge models; the `XsiSimObj` phases and the equivalence obligation are shared. |

**A line the authoring page must carry**, because it is the boundary that keeps this feature small:

> An edge may own **transport** — rate, buffering, ordering, loss accounting. It must not own **signal
> processing**. Every behavior here is reproduced by hand in C++ and nothing checks the two agree, so
> the bar is "obviously the same in ten lines". A filter is not that; put it in a block.

And its operational form, which is what actually catches cases — the rule above is a judgement call,
this one is a grep:

> **If the edge can only *record* a quantity and never *apply* it, it does not belong on the edge.**

It has caught three candidates so far, all of which looked like transport properties: gain, delay, and
per-channel skew. The last one had already shipped as a `t0` vector on `RFSampIF` before anyone asked
who read it — the answer was `min()` and a reporting accessor, so a design could declare skew the
model provably did not exhibit. A field whose only consumers are aggregates and getters is not
modelling anything, and a page that documents it is documenting a wish.

**Docs gates:** `tests/docs/test_markdown_integrity.py` (relative-link guard) and
`tests/docs/test_documented_numbers.py`.

## Verification

- The three XSI cycle gates (**158 / 176 / 2908**) unchanged through S1–S3 — no existing design has a
  behavioral edge, so any movement is a regression. **Run and unmoved.**
- S2's C++ test needs no Vivado. **It compiles and runs under a plain `g++`** — which is what the
  `xsi_simobj.h` split bought.
- S4's equivalence harness is the deliverable that makes every later edge cheap to trust. **Not
  built.** Until it is, "the two realizations agree on the counters" is asserted by reading both, and
  `guide/custom_hooks/behavioral.md` says so rather than implying the gate exists.

## Not in scope

- **Generating the C++ model from the Python `run_proc`.** Same anti-goal as `xsi_tb_codegen.md`
  Stage 0: the model is declared, not extracted. This is why the "ten lines" bar above exists.
- **Signal processing in edges.** See the boundary line.
- Edges that cross the cut. Those are boundary ports with BFM duals and are already built.

## Open questions

- ~~Does `BFM_DUALS` gain channel rows, or is a channel a separate table?~~ **Separate**, as the
  plan leaned. `xsi_channel_classes()` reads `xsi_channel.h`; `BFM_DUALS` is untouched. The reason is
  structural rather than stylistic: that table is keyed by the DUT's boundary port kind, and a
  model↔model edge has no such kind — a row would have nothing to key on.
- ~~Does an edge model need `pre_sim`/`post_sim` file I/O of its own?~~ **No** — see S1. Bundle I/O
  lives on nodes; `BlockChannel` carries no bundle machinery. A channel that logged its own traffic
  would be a monitor node attached to it.
- ~~Ordering when two behavioral edges are chained…~~ **Stated and measured.** One hop = exactly one
  cycle, pinned by `test_one_hop_costs_exactly_one_cycle`, quoted in
  `guide/custom_hooks/behavioral.md` and `guide/build/bfm.md`, and re-derived from the C++ by
  `tests/docs/test_documented_numbers.py` so the figure cannot rot. An N-hop chain adds N cycles that
  pysim does not have.

### Still open

- **Per-port `BfmModel` resolution**, so one module can bind a DUT port *and* a channel in one C++
  object. Refused loudly today; required by `plans/adc_model.md` stage 2 (the `Rfdc`).
- **The stale committed `xsi_bfm.h` copies** under `examples/*/xsi/` — see the S2 note.
