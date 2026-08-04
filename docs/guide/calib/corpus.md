---
title: The corpus — measured data
parent: Model calibration
nav_order: 2
audience: python
api: [CalibDataFrame, corpus_path, corpus_df, corpus_markdown, fit, get_params]
summary: "One canonical shape for measured data on both axes: corpus.csv, one row per measurement, a column per parameter and per target plus a timestamp. Derived on demand from each axis's raw tier — RTL runs for timing, synthesis reports for resources — so it can never go stale, and read by fit() as the default source."
---

# The corpus — measured data

Every model is fitted from measurements, and every model stores them the same way:

```text
<platform.dir>/models/<name>/corpus.csv
```

One file, one shape, both axes. What differs is only how the rows get there.

## The format

**One row per measurement.** Columns are:

| column group | what |
|---|---|
| one per **hardware parameter** | the design's resolved `HwParam` values — `width`, `ntap`, `dwid` … |
| one per **structural fact** | anything else that shaped the circuit — `xbar0_lanes`, `mem0_banks` |
| one per **runtime parameter** | workload inputs, where the axis has them — `nwords`, `num_trans` |
| one per **target** | what was measured — `lut`, `ff`, `dsp`, `bram` … or `residual` |
| `measured_at` | an ISO timestamp, stamped on append |

```text
dwid,vlen,lut,ff,dsp,bram,measured_at
64,4096,1370,599,4,4,2026-08-03T11:04:22
128,4096,2622,1399,8,8,2026-08-03T11:05:01
256,4096,6956,3622,16,16,2026-08-03T11:05:44
```

That is the whole contract. Columns are not fixed in advance: a model declares the order it prefers
and any extra column is kept, so a corpus can carry provenance a particular axis finds useful without
the format changing.

The **parameter columns are exactly what [`get_params`](./model.md#get_paramscomp-runtime--extract-and-record)
returns.** That is what lets one corpus serve fitting and prediction without a translation step in
between — and what makes a corpus inspectable: every row says what was built and what it cost.

## Raw facts, never derived ones {#raw-not-derived}

A row records what `get_params` extracted. It does **not** record what
[`transform`](./model.md#transformparams--derive) computed from it, and that separation is deliberate.

`transform` encodes a *modelling claim* — that LUTs go as `lanes²·log2(lanes)`, that area goes as
`a·b·c`. Claims get revised. If the corpus stored the derived term, a revision would strand every
measurement, because the inputs it was computed from would be gone.

{: .warning }
> Record `a`, `b`, `c` — never `area = a·b·c`. The derived column is cheap to recompute and
> impossible to invert.

So a column belongs in a row if it is a **fact about what was built**, whether or not it is a
`HwParam`. `VitisResourceModel` records its declared structure this way:

```text
dwid,vlen,mult0_count,mult0_operand_bits,mem0_banks,mem0_depth,mem0_elem_bits,xbar0_lanes,lut,ff,dsp,bram
128,4096,8,16,8,512,16,8,2622,1397,8,8
```

`xbar0_lanes=8` is the declaration; `xbar_sw=64` is the claim about it, and is derived at fit time.
Revise the cost dictionary and this row still fits.

The guarantee that keeps this honest is structural rather than editorial: `transform` receives the
parameter mapping and **never the component**, so a model cannot predict from anything a row does not
contain.

## It is derived, never authoritative

`corpus.csv` is a **view** of measurements that live elsewhere:

```text
raw tier  ──(axis-specific)──>  corpus.csv  ──(fit)──>  params.json
```

| axis | raw tier | who reads it |
|---|---|---|
| **timing** | `rtl/<run_id>/firings.csv` and `pysim/<run_id>/firings.csv` — one directory per run | the timing model joins the two on the feature point |
| **resources** | `modules/<key>/resource/records.jsonl` in a [record store](./modules.md) | the resource model flattens the records it is keyed for |

The raw tier is the ground truth. It carries **provenance** — which synthesis, which part, which tool,
how long it cost, and a signature of the module it describes — and that provenance is checked on read.

{: .note }
> **Why derived rather than maintained.** A corpus updated incrementally can drift: one process files
> a measurement, another holds a stale table, and nothing notices. Regenerating from the raw tier
> makes that impossible — the corpus is always exactly what was measured, deleting it is never data
> loss, and the cost is parsing a few dozen reports.
>
> The corollary is that you should never hand-edit `corpus.csv`. Correct the raw tier and regenerate.

Generating it is the one part that cannot be shared, because only the axis knows how to read its own
raw tier: a `csynth.xml` attribution on one side, a firings join on the other.

## Reading it

```python
model.corpus_df          # -> pandas.DataFrame, for analysis
model.corpus_markdown()  # -> a markdown table, for a report or an AI context
```

`corpus_df` is the working form — filter, group and plot with native pandas. `corpus_markdown` exists
because a corpus is *evidence*: the numbers behind a model belong in the document that makes a claim
about it, and in the context window of anything reasoning about the design.

## Fitting reads it by default

```python
model.fit()              # reads corpus.csv — the normal path
model.fit(samples)       # explicit override, for a test or a one-off
```

Passing samples is deliberately **not** the default. A fit that quietly bypassed the recorded corpus
would produce a model whose provenance nobody can reconstruct — the parameters would exist with no
answer to "fitted on what?".

## How many rows do you need?

That is a **coverage** question, and it is what separates the model kinds:

| model | rows needed |
|---|---|
| a memorizing lookup | **one per configuration you will ever query** — it does not generalize |
| a fitted model | enough to pin its coefficients, then it covers the space between them |

The trade is bias against coverage. A lookup assumes nothing about the shape of the function, so
nothing it assumes can be wrong — but it answers only where you measured. A fitted model commits to a
shape and, in exchange, answers between the points. `examples/vecmult` fits four coefficients from 15
rows and predicts anywhere in the region; a lookup over the same space would need every point.

## The trap: rows keyed to a module that was never wired

A resource row is keyed by the module's **elaborated structure**, and a module with ports has no
settled structure until those ports are wired into a harness.

{: .warning }
> Building rows from components elaborated **standalone** gives keys no real composite ever produces.
> Every later prediction then misses — each module reporting `UNCALIBRATED`, contributing zero, and
> making the design read as *cheaper* than it is.
>
> It fails silently, because a missing key looks exactly like a configuration you never measured.
>
> Generate the corpus from the raw tier (whose records were filed from wired instances), or take
> components out of an assembled design.

## Status

{: .note }
> Built and in use: the format (`CalibDataFrame`), the `corpus_df` / `corpus_markdown` accessors,
> `fit(data=None)` reading the corpus by default on both axes, the `get_params` / `transform` split,
> and both generators — timing regenerates from its raw runs on every fit, and
> `corpus_from_records(store, cls_name=...)` reduces the resource record store.
>
> Not yet wired: nothing calls `corpus_from_records` automatically on the resource side — a model
> still receives explicit samples. Making `fit()` reach for it is tracked in
> `plans/harmonize_calib.md`.

## Next

- [`CalibDataFrame`](./dataframe.md) — the object that implements this format.
- [Confidence](./confidence.md) — what a model says about a prediction once it is fitted.
