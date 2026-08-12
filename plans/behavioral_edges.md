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

### S1 — `xsi_model()` as an `Interface` hook

Declare the hook and its resolver, mirroring `bfm_model()`: `declares_hook(iface, "xsi_model")`, a
`ChannelModel` record (C++ class, the two peer endpoints in constructor order, `extra_args`), and
`DynParam` config emission. No emitter changes yet.

**Gate:** unit tests only; the four XSI designs unchanged (none declares the hook).

### S2 — the C++ channel primitive

A `wfbfm::BlockChannel<T>` in `waveflow/build/xsi/`: a bounded deque with the phase discipline
(`update()` publishes, next `sample()` observes), a depth, and **drop / starve counters** so an overrun
or underrun is a number rather than a silent behavior. Plus the fractional-credit accumulator as a
reusable `RateTick` helper.

**Gate:** a standalone C++ test, driven the way `tests/build/test_xsi_bundle_io.py` drives the bundle
round-trip — no Vivado needed, `g++ -fsyntax-only` plus a run.

### S3 — the second walk in `tb_top_spec`

Today's walk iterates the DUT boundary. Add a second pass over the TB's interfaces whose endpoints are
**both** outside the cut, emitting a channel and binding the two peer models to it. Declaration order
must place a channel before both of its peers (the harness already orders shared objects first).

The two walks must not double-count: an interface with one endpoint on the DUT boundary is the existing
case and stays there. An interface with *neither* endpoint on a participant is an error, not a no-op.

**Gate:** all existing designs byte-identical (none has a non-boundary edge); a new minimal two-model
fixture emits a channel, compiles, and moves a value between two participants across a cycle boundary.

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
| `guide/interface/overview.md` | edit | The axis this adds: an interface is not only a *wiring* record — it may carry **behavior and state** (`StreamIF.depth` already does; a behavioral edge adds a `run_proc`). Name the two hooks side by side so `bfm_model()` (node) and `xsi_model()` (edge) are learned together. |
| `guide/interface/index.md` | edit | One line in the section index pointing at the new page below. |
| `guide/interface/behavioral.md` | **new** | The authoring page for a behavioral edge: when an edge deserves behavior *(rate conversion, buffering, loss accounting — not signal processing, see below)*; the `run_proc` half; the `xsi_model()` half; the queue phase discipline and why a direct call is wrong; the counter contract; the equivalence obligation and its gate. |
| `guide/build/bfm.md` | edit | The model library page currently implies every model binds RTL pins. Add the channel: models may bind **each other**, `BlockChannel` is the primitive, and the write-in-`update` / read-in-next-`sample` rule is what makes it order-independent. |
| `guide/comp_codegen/xsi_tb.md` | edit | `tb_top_spec` now has **two walks** — the DUT boundary (one model per port) and non-boundary edges (one channel per edge). State which walk claims which interface, and that an interface reaching neither is an error. |
| `guide/custom_hooks/bfm_model.md` | edit | A cross-reference: this page teaches node models; `guide/interface/behavioral.md` teaches edge models; the `XsiSimObj` phases and the equivalence obligation are shared. |

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
  behavioral edge, so any movement is a regression.
- S2's C++ test needs no Vivado.
- S4's equivalence harness is the deliverable that makes every later edge cheap to trust.

## Not in scope

- **Generating the C++ model from the Python `run_proc`.** Same anti-goal as `xsi_tb_codegen.md`
  Stage 0: the model is declared, not extracted. This is why the "ten lines" bar above exists.
- **Signal processing in edges.** See the boundary line.
- Edges that cross the cut. Those are boundary ports with BFM duals and are already built.

## Open questions

- Does `BFM_DUALS` gain channel rows, or is a channel a separate table? It is keyed by *DUT port kind*
  today, which a model↔model edge does not have — leaning separate.
- Does an edge model need `pre_sim`/`post_sim` file I/O of its own (a channel that logs its traffic to a
  bundle), or does that belong to a monitor node attached to it?
- Ordering when two behavioral edges are chained: the queue discipline makes each hop take one cycle,
  so an N-hop chain adds N cycles of latency in XSI that pysim does not have. Real, and probably
  acceptable — but it must be *stated*, since pysim and XSI already disagree on timing by design.
