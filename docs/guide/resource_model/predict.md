---
title: Predicting
parent: Resource Models
nav_order: 6
audience: python
api: [predict, compose, ResourceEstimate, Confidence, boundary_signature]
summary: "Getting a number out: predict for one module, compose for a hierarchy. The composition rule is one line applied recursively — a module's own cost plus the sum of its children — where a composite's own cost is the interface term, and that term is defined exactly as a synthesis report measures it. Every estimate carries the weakest confidence that fed it, and names the modules sitting at it."
---

# Predicting

## One module

```python
top = elaborate(VecMult, {"dwid": 64, "vlen": 4096}, name="vec_mult")
top.add_rm(platform)
top.resource_model.predict(top)
# {'lut': 1370, 'ff': 598, 'dsp': 4, 'bram': 4, 'uram': 0, 'srl': 0}
```

`predict` takes the **component**, not a feature vector — see
[why](./rm.md#takes-a-component). It returns that module's *own* counters,
never its children's.

## A hierarchy

```python
est = compose(top)

est.total        # {'lut': …, 'ff': …, 'dsp': …, 'bram': …}
est.level        # the WEAKEST confidence that fed it
est.weakest()    # which modules sit at it — what you would recalibrate first
est.per_module   # [(path, cls_name, counters, Confidence), ...]
```

### The composition rule

One line, applied recursively:

```text
predict(comp)  =  comp's OWN model  +  Σ predict(child)
```

A **leaf's** own cost is its whole cost. A **composite's** own cost is what it adds *beyond* its
children — the `m_axi` adapters, the inter-task FIFOs, the AXI-Lite control block, the DATAFLOW shell.

That is not a third term bolted onto a per-module sum: it is the same rule one level up, and it is
exactly what a synthesis report measures as `top row − Σ task rows`
([Composite kernels](../resource/composite.md)). Definition and measurement coincide with nothing left
over, which is what makes the model checkable against a whole-design run.

Nothing is passed down. Each model reads its own features off the component it is attached to, because
[elaboration](../comp_codegen/elaborate.md) already resolved every child's parameters — so a child's
features cannot drift from the design that was synthesized.

### The interface term {#the-interface-term}

A composite's own cost comes out by subtraction only because Vitis declines to itemize it. It is in
one-to-one correspondence with the design's interface graph:

| unreported RTL | comes from |
|---|---|
| `gmem<n>_m_axi` | one per `m_axi` boundary port |
| `fifo_w<W>_d<D>_S` | one per **internal** task-to-task channel |
| `control_s_axi` | the ap_ctrl / AXI-Lite block |
| `entry_proc`, `regslice_both`, `sparsemux_*` | the DATAFLOW shell |

So `InterfaceResourceModel` keys on a `boundary_signature` — port kinds and widths, channel widths —
rather than on parameters. The evidence is two-sided: across 24 points varying the compute parameters
the term never moved, and it *did* move when the memory word width changed, identically for both
realizations at each width.

{: .note }
> The term can be **negative** when HLS shares logic across a module boundary. Nothing clamps it. A
> negative own-cost is information rather than an error — it is exactly the cross-block surprise that
> whole-design synthesis exists to catch, and it is invisible if modules are only ever measured
> standalone. `VecMult` shows a small one: `lut: -2`.

## Confidence {#confidence}

Every model returns a `Confidence` beside its counters, and a composed estimate reports the
**weakest** one:

| level | means |
|---|---|
| `EXACT` | the form reproduces every calibration point with zero residual — a checked claim |
| `INTERPOLATED` | the query lies inside the region the model was fit over |
| `EXTRAPOLATED` | outside it — and what you cross on the way out is usually a *regime boundary* |
| `UNCALIBRATED` | no fit backs this number |

A `VitisResourceModel` illustrates why the weakest link is the right rule. Two of its counters are
zero-parameter rules that reproduce every measurement; two are regressions. The composed verdict is
**`INTERPOLATED`**, not `EXACT`:

```text
total   {'lut': 1370, 'ff': 597, 'dsp': 4, 'bram': 4}
level   INTERPOLATED
weakest [('vec_mult', 'VecMult')]
```

An estimate reporting `EXACT` while half of it is a fit would be the most misleading thing this layer
could do.

The facts carry the reasons, not just the level — for that module: *"lut: form reproduces all 15
calibration points exactly (4 free parameters); ff: inside the calibrated region; worst residual on
the corpus was 1.3%"*.

`EXTRAPOLATED` deserves particular attention on this axis. Leaving the measured region usually means
crossing a *binding* threshold — DSP-versus-LUT inference, block-RAM-versus-LUTRAM partitioning — and
those move several counters at once rather than degrading smoothly.

## What a missing model does

A module with no model contributes zero **and** reports `UNCALIBRATED`, naming itself. It is never
silently skipped:

```python
Confidence.uncalibrated("Foo has no resource model; its cost is missing from this estimate, not zero")
```

Under-counting is the one direction an estimate must not err, because it turns "does not fit" into
"fits". The same rule covers a counter a model does not predict: reported by name, never defaulted to
zero.

## Checking an estimate against reality

The composed total is comparable to a whole-design synthesis report directly — that is the point of
the interface term being defined as the report measures it. See
[Composite kernels](../resource/composite.md) for getting the measured side, and
[Fitting](./fit.md#validating) for what to compare and what "close enough" should mean.

## Next

- [Fitting](./fit.md) — where the coefficients come from, and how to validate them.
