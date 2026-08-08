---
title: The interface model
parent: Resource Models
nav_order: 6
audience: python
api: [InterfaceResourceModel, boundary_signature, boundary_text, LookupResourceModel]
summary: "What a composite costs beyond the sum of its sub-modules — the m_axi adapters, inter-task FIFOs, control block and DATAFLOW shell. It is a LookupResourceModel keyed on the boundary signature rather than the module key, because that is what the term was measured to depend on: invariant across compute parameters, moving only when the ports changed. A lookup rather than a fit is a statement about the evidence, and the term is never clamped at zero."
---

# The interface model

A synthesis reports a figure per task **and** a figure for the whole design, and they are not the
same number. The difference is what the composite costs on its own:

```text
integration  =  top  −  Σ(modules)
```

That term holds the `m_axi` adapters, the inter-task FIFOs, the AXI-Lite control block and the
DATAFLOW shell — everything the design needs to *be* a design rather than a bag of modules. On
[`fir_block`](../../examples/firblock/resource_model.md) it is **1984 LUT, 29 % of the design**: the
second-largest contributor after the compute, and far too large to leave out of an estimate.

## It is a lookup with a different key

`InterfaceResourceModel` is a [`LookupResourceModel`](./lookup.md) subclass, and that is the whole
implementation. Memorizing, refusing to interpolate, the `EXACT`/`UNCALIBRATED` transition, the
artifact round-trip — all inherited. What it specializes is the **identity**:

| model | keyed on |
|---|---|
| `LookupResourceModel` | the module key — the module's elaborated structure |
| `InterfaceResourceModel` | the **boundary signature** — the composite's ports and channels |

```python
basis = ["boundary"]        # instead of ["module_key"]
```

So the two kinds are one kind asked about two different things. Recognising that is what keeps the
interface term from becoming a parallel implementation of a table: a bug fixed in the lookup is fixed
in both, and a report that can summarize one can summarize the other.

## Why the boundary is the right key

Because that is what the term was **measured** to depend on, and the evidence is two-sided:

- across 24 points varying `ntap`, `samp_w` and the realization, the term never moved;
- it *did* move when `mem_dwidth` changed — identically for both realizations at each width.

A term keyed on the composite's parameters would have had 24 entries where the hardware has two. A
term keyed on nothing would have missed the `mem_dwidth` dependence entirely.

The signature is `(ports, channels)`: each port as `(kind, width)`, each channel likewise, both
sorted so port order is not part of the identity. Neither is a `HwParam`, which is why they are read
in `get_params` — extraction, so they land in the corpus. A term whose boundary was never recorded
could not be re-derived from the measurements it was built from.

{: .note }
> The key is stored as **text** (`boundary_text`), not as the nested tuple. A lookup key has to
> survive a CSV round-trip: the same boundary written by a live component and read back from a corpus
> must land on one entry, and a nested tuple does not survive that trip while its `repr` does. The
> `ports` and `channels` columns are kept **alongside** it, because the key is a modelling choice that
> may be revised and they are the evidence it was derived from.

## Why a lookup rather than a fit

This is a statement about the evidence, not a limitation of ambition. The natural next form is a
per-port decomposition:

```text
Σ adapter_cost(kind, width)  +  Σ fifo_cost(width, depth)  +  shell
```

and the signature is shaped to support exactly that. But separating those coefficients needs more
boundary configurations than have been measured, and fitting them from two points would be inventing
structure rather than finding it.

## Building the table

The composite's own cost is a measurement like any other, so it is **filed** rather than transcribed:
one `integration` record per synthesis, deduplicated by boundary.

```python
InterfaceResourceModel(name="shell", store=store, cls_name="VecMult").load_table()
```

That dedup is what makes the invariance *derivable* rather than asserted: a 24-point sweep files 24
records, and if they agree the table has one entry.

{: .warning }
> **A boundary carrying two different measurements raises.** That is a contradiction in the data, not
> something to average: either the term is not a function of the boundary alone, or two designs were
> filed against one platform. Both need a person, and picking one quietly would bury the finding the
> store exists to surface.

A model with no store keeps whatever table it was given, which is what lets a design whose
measurements have not been filed yet supply one directly.

## The term is never clamped at zero

On a single-task design the term can come out **negative** — on
[`vecmult`](../../examples/vecmult/resmodfit.md#integration-term) it is `-2` LUT, the tool optimizing
two LUTs away as it flattens the only instance into the top.

Nothing clamps that. A negative own-cost is the signal that additivity is leaking across a module
boundary, and hiding it would hide exactly what whole-design synthesis exists to catch.

The practical consequence is that module models are fitted on **module** figures with this term added
back, rather than fitted on design totals with the difference silently absorbed into their
coefficients. Same prediction either way on one task; only one of them stays right when a second task
appears.

## Next

- [Predicting](./predict.md) — how the term joins a composed estimate.
- [The lookup model](./lookup.md) — the machinery this inherits.

## See also

- [Block FIR resource models](../../examples/firblock/resource_model.md) — where the term is 29 % of
  the design.
- [VecMult's resource model](../../examples/vecmult/resmodfit.md#integration-term) — where it is `-2`.
