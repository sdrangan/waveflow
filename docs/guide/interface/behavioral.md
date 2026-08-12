---
title: Behavioral edges
parent: Interfaces
nav_order: 9
audience: python
api: [Interface, xsi_model, ChannelModel, BlockChannel, RateTick, declares_hook, tb_top_spec]
summary: "An interface is not only a wiring record — it may carry behavior and state. This page is the authoring guide for a behavioral edge: when an edge deserves behavior (rate, buffering, ordering, loss accounting — never signal processing), the run_proc half that makes it work in pysim, the xsi_model() half that gives it a C++ realization, the queue phase discipline that makes a model-to-model transfer independent of participant order, the counter contract, and the equivalence obligation the two halves owe each other."
---

# Behavioral edges

Most interfaces are a **wiring record**: they say which master talks to which slave, and the
transaction methods do the rest. Some are more than that. `StreamIF.depth` is already a *physical*
property owned by the edge and read by both backends — pysim bounds its queue with it, codegen emits
`#pragma HLS STREAM depth=N`.

A **behavioral edge** goes one step further: it has a `run_proc` of its own. It is still an
[`Interface`](./overview.md), and an `Interface` is already a [`SimObj`](../sim/), so the Python half
needs no new machinery at all. What was missing was the other half — a C++ realization — and that is
what this page is about.

## Two hooks, learned together

A **node** declares how it is realized; so does an **edge**. The two are exact peers:

| | module (node) | interface (edge) |
|---|---|---|
| pysim | `run_proc` on a `HwModule` | `run_proc` on an `Interface` |
| XSI | [`bfm_model()`](../custom_hooks/bfm_model.md) → an `XsiSimObj` bound to **RTL pins** | `xsi_model()` → an `XsiSimObj` bound to **two peer models** |

Both are optional, both are **declared and never derived**, and both are detected by identity rather
than by `hasattr`:

```python
declares_hook(iface, "xsi_model")     # False for the base Interface, True for an override
```

`hasattr` cannot answer this and must not be used for it. The moment `Interface.xsi_model` exists as
a base method it is `True` for *every* interface — so an emitter probing with it would treat every
`StreamIF` in every design as a behavioral edge. (The same trap was found one level up, on
`bfm_model`, and the fix is the same predicate.)

## When an edge deserves behavior

An edge may own **transport**: rate, buffering, ordering, loss accounting.

It must **not** own **signal processing**. The reason is not taste — it is that everything here is
written twice, once in Python and once in C++, and *nothing checks that the two agree*. So the bar is
**"obviously the same in ten lines"**. A bounded queue plus two counters clears it. A filter with
fractional delays and a phase accumulator does not: you would be proving a DSP library bit-exact
against NumPy, by hand, with no gate.

There is a sharper, operational form of the same rule, and it has now caught three candidates (a
scalar gain, a bulk delay, a per-channel skew):

> **If the edge can only *record* a quantity and never *apply* it, it does not belong on the edge.**

It is checkable rather than a matter of judgement: grep for who reads the field. A quantity nobody
applies is worse than an absent one, because an accessor reports a behavior the model does not
exhibit.

Signal processing goes in a block. Adding one later is purely additive — a new module, no interface
change, no C++ change, no re-gated model. Removing behavior from an interface later means rewriting
its C++ model and re-verifying its gate. "Add it later" is true in one direction only.

## The pysim half

Nothing new: give the interface a `run_proc` and whatever state it needs.

```python
@dataclass
class TokenIF(Interface):
    depth: int = 4

    def __post_init__(self):
        self.endpoint_names = ('tx', 'rx')
        super().__post_init__()
        self.q = simpy.Store(self.env, capacity=int(self.depth))
        self.dropped = 0
```

`Simulation.run_sim` schedules `run_proc` for every registered `SimObj`, and an `Interface` is one,
so an edge's process starts exactly as a module's does.

## The XSI half

`xsi_model()` returns a `ChannelModel` — the edge-side twin of a `BfmModel`:

```python
    def xsi_model(self) -> ChannelModel:
        return ChannelModel("BlockChannel<uint64_t>", peers=("tx", "rx"),
                            extra_args=(str(self.depth),))
```

| field | is | note |
|---|---|---|
| `cls` | the C++ channel class | template args are part of the name; the registry checks the base |
| `peers` | this interface's **side names**, producer first | the keys of `Interface.endpoints` |
| `extra_args` | literal ctor args | what the channel needs and the graph does not carry |

`peers` are *side* names, not attribute names — and that asymmetry with `BfmModel.ports` is worth a
sentence. A module's `ports` have to be attribute names because C++ constructor order is recorded
nowhere else. An interface **owns** its sides, so the naming problem does not arise.

The class must exist in `waveflow/build/xsi/xsi_channel.h`, and that is checked by reading the
header — not against a Python list, which would be a second copy of the library and would drift the
first time a channel was added.

`DynParam` fields on the interface are emitted as member assignments, exactly as a model's are:
`<channel>.<field> = <expr>;`. The same obligation applies as on the node side — the field has to
exist on the C++ class, and nothing static checks that.

## Why a queue, and not a direct call

This is the load-bearing part of the design.

A harness drives **one participant list** through five phases per cycle. If model A simply called
model B, whether B saw the value this cycle or next would depend on which of them the harness
happened to visit first — a generator-ordering detail deciding a functional result.

So a channel **stages**. `push()` puts the item aside; the channel's own `sample()` commits it into
the readable queue. The channel is declared, constructed and registered **before both of its peers**,
so its `sample()` runs first in every sweep. The consequence:

> An item pushed at any point in cycle *c* becomes visible at the start of cycle *c+1*, and never
> within cycle *c* — whatever order the peers appear in.

It is the same reason `sample()` and `update()` are split in the pin-level models: a transfer is
decided from values observed *before* the clock edge and applied *after* it.

Two consequences follow, and both are stated rather than left to be discovered:

- **Each hop costs exactly one cycle** that the pysim graph does not have, *whichever phase a peer
  reads in* — that uniformity is what the channel-first rule buys, and it is why the rule is asserted
  in all three places rather than left to declaration luck. An N-hop chain adds N cycles in XSI.
  Real, and by design — the two backends already disagree on timing.
- **Ordering is a property, not a convention.** `tests/build/test_xsi_channel.py` runs the same
  producer/consumer pair in both registration orders and requires identical transcripts, and it is
  verified to fail if the staging is removed.

## The counter contract

`BlockChannel` carries three numbers, and they are the point rather than bookkeeping:

| counter | means |
|---|---|
| `transferred` | successful reads |
| `dropped` | pushes refused because the channel was full |
| `starved` | reads that found it empty |

A loss nobody can read is the "deadlock looks like success" failure in a new costume: a graph that
silently dropped half its traffic still finishes, still produces well-formed output, and still passes
every functional check on the data that did arrive. The counters are what make that a number.

The depth bounds **committed plus staged** items. If staging did not count, a producer could push any
number of items within one cycle and the depth would apply only *between* cycles — a bound that is
not a bound.

### Rate conversion belongs to the edge

A behavioral edge typically runs on its own clock while the harness steps on the fabric clock, and
the conversion between them is a **ratio, not a count**. `RateTick` is the fractional-credit
accumulator:

```cpp
credit += ratio;                       // derived: f_edge / f_axis
if (credit >= 1.0) { credit -= 1.0; /* one edge-tick this cycle */ }
```

Derived, never declared: both frequencies already exist elsewhere, so a `ticks_per_cycle` parameter
would be a third statement of a quantity the design already fixes twice. A ratio above 1 aborts
rather than silently losing ticks — it means the port cannot carry the rate, which is a design error.

## The equivalence obligation

Two realizations, hand-written twice, and **nothing checks that they agree**. That is the standing
obligation of every behavioral edge, and it is why the "ten lines" bar exists at all.

The gate that would discharge it — running one scenario through both backends and requiring identical
counter tuples — is **not built** (`plans/behavioral_edges.md` S4). Until it is, the counters agreeing
is something you assert by reading both implementations, and this page is not evidence that they do.

## What is deliberately not here

- **Generating the C++ model from the Python `run_proc`.** The same anti-goal as the node side:
  nothing can extract a cycle-exact model from SimPy behavior, and the ten-line bar exists precisely
  so that it does not need to.
- **Multi-producer / multi-consumer channels.** One producer, one consumer until something needs
  more.
- **Instrumentation edges** (log / count / inject backpressure). Free once the mechanism exists; not
  designed yet.

## See also

- [BFM testbenches](../build/bfm.md#channels) — the model library, and the channel beside it.
- [XSI testbench in HLS](../comp_codegen/xsi_tb.md#two-walks) — which of `tb_top_spec`'s two walks
  claims which interface.
- [Writing a BFM model](../custom_hooks/bfm_model.md) — the node-side authoring page; the
  `XsiSimObj` phases and the equivalence obligation are shared.

**Source of truth:** `waveflow/hw/interface.py` (`xsi_model`),
`waveflow/build/xsi/xsi_channel.h` (`BlockChannel`, `RateTick`),
`waveflow/build/composite_gen.py` (`ChannelModel`, `resolve_channel_model`).
