# Plan: `docs/examples/rf_loopback` — restructure as a walkthrough

**Status: phase 1 DONE, 2026-08-16** (branch `rf-docs-restructure`). See *What landed* — including
**two stale claims the audit below missed**, which is the more interesting result.

**Phase 2 — capture and replay — NOT STARTED.** It adds two learning objectives to the example and a
third flow to `guide/flows`. It is at the foot of this file and it is the larger piece.

## The audit, and it corrected my assumption

I expected staleness: the page was written at stage 1, before the two-task overlap, the model-fidelity
fixes, the DAC grid change, and `loop_blk_latency = 1 + dut.blk_latency`. **It is not stale.** Spot
checks against the code all pass:

| claim on the page | status |
|---|---|
| `dac {... 'underrun': 2 ...}` | correct — `loop_blk_latency` is 2 |
| the 1066 RTL gate, and the 1072 → 1066 explanation | correct, and the reasoning is intact |
| the two-task split, with the real generated C++ | current |
| `(8,8) (16,4) (12,4) (16,2)` width sweep | matches `test_rf_loopback.py` |
| "dropped 72 of 512 samples" | correct as *history*, and labelled as such |

**Why it survived:** `tests/docs/test_documented_numbers.py` already covers this page's loss counts,
RTL completion cycle, scenario size, and `ADC_DROPPED`/`ADC_WORDS`. The numbers are machine-checked,
so they could not rot. That is the guard earning its place, and it is the argument for extending it to
any number a new page quotes.

So this is **not a rescue**. It is the same critique the guide got: the page is organised around *the
design and its proof*, not as something you can follow while building. Plus one genuine gap — the
sine waveform is new and undocumented.

## The shape

The guide **orients**; this example is where a reader follows along. So: steps, with a figure wherever
a figure carries the claim better than a sentence.

```
docs/examples/rf_loopback/
  index.md    what the example is, the domains picture, and what each page covers
  build.md    1. the Rfdc      2. source and sink   3. wiring   4. the DUT
  run.md      5. the three claims                   6. the deliberate faults
  rtl.md      7. csynth, XSI, and the 1066 gate
```

Splitting `python.md` (359 lines, covering pysim *and* the whole RTL half) is most of the win by
itself — "build it" and "take it to RTL" are different sittings.

### Steps, and the figure each earns

1. **Create the `Rfdc`.** Parameters and which kind each is; the four ports and which two cross into
   the fabric. *Figure: the existing `rf_domains.svg`, reused from the guide.*
2. **Create the source and sink.** File-backed, and the bundle discipline — one on-disk file drives
   Python *and* the later RTL run. ***Figure: the source data alone*** — the windowed sinusoid, so a
   reader sees what is going in before anything happens to it.
3. **Wire the graph.** Four edges: `RFSampIF` for the RF domain, ordinary `StreamIF` for the fabric.
4. **The DUT, and why it is two tasks.** The 72-dropped-samples history belongs here and nowhere else,
   as one sentence of evidence for the rule.
5. **Run it and check.** The three claims. *Figure: the two-panel loopback plot* — the shift and the
   zero-fill, visible rather than asserted.
6. **The deliberate faults.** Late producer, stalled consumer. ***Figure: the late-producer run*** —
   the zero-filled blocks are legible in a way `underrun == 2` is not.
7. **To RTL.** csynth, the XSI run, the 1066 gate, `ADC_DROPPED == 0`.

Three plots, all from `rf_loopback_figures.py` — one script, three outputs, each a **measurement**
rather than a drawing. A stale plot is a claim nothing checks, so they regenerate together.

## What to add

- **The sine waveform.** New and undocumented, and the reason it exists is worth a paragraph: the grid
  waveform makes `from_real` a no-op, so it never exercises rounding or saturation. Two waveforms
  because they test different things.
- **Numbers → the guard.** Every number a new page quotes goes into
  `test_documented_numbers.py`. That is why this page survived six months of change; extend it rather
  than admire it.

## What to cut

The editorial rule from the guide plan applies here too, more gently — this is the example, so it is
*allowed* more narrative than a how-to page. But the proving stories still compress: **a claim earns a
sentence of evidence, not a section.** The 1072 → 1066 discussion is a good model of the right length
already.

## Gates

`tests/docs/test_markdown_integrity.py` (splitting a page breaks inbound links — the guide's quickstart
points at `python.md` by name, and that link must be updated in the same commit) and
`test_documented_numbers.py`.

## Open question — resolved

Does the guide's quickstart cite `build.md` or `index.md`? **`index.md`**, which survives further
splits; the sub-pages are listed there in a table.

---

## What landed

The split, exactly as designed: `index.md` (the domains picture and a page map), `build.md` (steps
1–4), `run.md` (steps 5–6), `rtl.md` (step 7). `python.md` is deleted.

**Mostly relocation, and it should be read that way.** `build.md` and `rtl.md` are `python.md`'s
prose moved and re-sectioned, with the wiring and bundle material promoted into steps of their own.
Genuinely new writing: `index.md`'s page map, `build.md`'s *two waveforms* section, `run.md`'s
zero-fill table and its late-producer commentary, and the corrected `check` block on `rtl.md`.

### Three figures, one script

`rf_loopback_figures.py` now renders all three and pins `svg.hashsalt`, so a regeneration that
changes nothing is byte-identical (it was not before — matplotlib salted element ids with a uuid4).

| figure | step | what it measures |
|---|---|---|
| `rf_source_sine.svg` | 2 | the source's own data, read back **out of the bundle it was written to** |
| `rf_loopback_sine.svg` | 5 | the loop's 2-block shift and the leading zero-fill (unchanged) |
| `rf_late_producer.svg` | 6 | on-time vs late capture: 2 leading flat blocks becomes 4 |

The late-producer figure uses the **grid** waveform, not the sine, and that is not a stylistic
choice: every grid block is full-scale, so a flat block at the sink can only be zero-fill. With the
sine, the leading zero-fill and the closed half of the window are indistinguishable and the figure
would prove nothing.

### Two stale claims the audit above missed

The audit's conclusion — *"the numbers are machine-checked, so they could not rot"* — was right about
the numbers it named and **wrong as a verdict on the page**, because two load-bearing claims were not
in the guard:

1. **"the sink's first three blocks are all zeros"** under the late producer. The run produces
   **four**, and `tests/examples/test_rf_loopback.py` has asserted `range(4)` plus
   `captured[4] == sent[0]` the whole time. A green test and a wrong page, side by side.
2. **`check(RfDataSource, "xsi_bfm_model") -> False`** as the "declares neither hook" example. The RF
   environment nodes acquired `bfm_model()` hooks; `test_rf_environment_nodes_now_declare_their_models`
   documents the inversion explicitly. The module that carries that finding today is the **DUT**,
   which declares no `bfm_model()` because it belongs *inside* the cut.

Both are now in `test_documented_numbers.py`, the second by executing `check` and `potential_targets`
rather than by matching prose. The lesson is narrower than "guards work": a guard covers the
*claims it names*, and the two that rotted are precisely the two nobody had written a line for.

### The guard, extended

`tests/docs/test_documented_numbers.py`: 17 → 21 tests. New coverage — the zero-fill table (both
rows, as whole table cells), the source figure's block/window geometry, the grid-vs-sine quantizer
claim (run, not asserted in prose), and the two `pycon` blocks on `rtl.md`. Existing RF tests
repointed from `python.md` to `run.md` / `rtl.md`.

### Numbers deliberately dropped

`python.md`'s *"136 cycles per firing … ~133 either way"* is gone. It could not be checked without a
csynth run and nothing recomputed it, so by the rule that governs this work it should not be on a
page. The argument it supported survives without it: the block stage still runs two sequential
pipelined loops over one block RAM, so the split was never about speed.

### Gates

`tests/docs` 21 passed. Dev loop `6 failed, 2701 passed, 2 skipped, 168 deselected` against a
measured baseline of `6 failed, 2697 passed, 2 skipped, 168 deselected` — same six pre-existing
failures (`test_dataschema_poly` ×1, `poly/test_timing_analysis` ×5), +4 new guard tests. `-m xsi`
not run: no example code changed, only the figure script.

### Left for someone with authority to change code

Six docstrings in `examples/` and `waveflow/` point at `docs/guide/rf/fidelity.md`, which the guide
restructure moves to `docs/guide/rf/python/fidelity.md`. They are prose in `.py` files, not links,
so no guard sees them — and this work was scoped to docs.

---

# Phase 2 — capture and replay

**Status: NOT STARTED.** Design agreed 2026-08-16; nothing built.

## The question this answers

`python/fidelity.md` now ends by naming what pysim cannot see, and the honest reading of it is
uncomfortable: **neither backend can run the test you actually want.** pysim runs a rich RF
environment — a channel model, a windowed burst, milliseconds of signal — but cannot see a
sub-block stall. XSI sees every cycle but cannot run any of that environment, and building an XSI
model of a channel is work nobody should do.

A reader who reaches the end of that page is entitled to ask *"so what do I actually do?"*
Capture-replay is the answer, which makes it the **payoff of the arc** rather than a technique bolted
on afterwards.

## The two learning objectives

Added to `examples/rf_loopback`, alongside the ones it already carries:

- **Capture** the quantized samples crossing the `Rfdc`'s fabric boundary — both directions — during a
  Python simulation, as RTL test vectors.
- **Replay** the captured input at RTL, capture the RTL output, and compare it against the Python
  output for functional validation.

## Why this belongs in `guide/flows` and not only here

The methodology is not RF-specific. It is what you do at **any** boundary whose far side you refuse to
model at RTL, and the RF converter is simply the first place in this repo that had a compelling reason
to need it. Network adapters, GPIO, anything with a physical environment — same shape.

### The definition that makes it generalize

> **A peripheral is a boundary whose far side you refuse to model at RTL.**

Not "a block that does I/O" — that is most blocks. The defining property is the *refusal*: the
environment is expensive or impossible to simulate cycle-accurately, so you don't, and everything else
follows mechanically from that one sentence.

1. **You must capture at that boundary** — nowhere else has the data.
2. **You must replay *through* the peripheral, not around it.** Otherwise you lose its timing
   behaviour. This is the gotcha that will bite every user of the flow; see *Four gotchas* below.
3. **The peripheral model must be bit-exact across backends**, or comparing a replay against a
   recording proves nothing. Every quantizer conformance test in this arc exists to make this step
   legitimate — that is what they were *for*, retroactively.
4. **The cut goes at the peripheral**, so the RTL graph is logic + peripheral, environment absent.

### It is a third flow, and the index needs one sentence changed

`guide/flows/index.md` defines a flow as *"the end-to-end recipe … which build steps run, in what
order, producing which artifacts, and how the result is checked."* Capture-replay changes all three: a
capture step, recorded vectors as artifacts, comparison-against-recording as the check. What excludes
it is the *next* sentence — *"they split on one axis: the DUT"* — and the DUT here is unchanged.

Amend the axis rather than demote the pattern to a subsection:

> Flows 1 and 2 split on **the DUT's control model**. Flow 3 splits on **whether the environment is
> realizable at RTL at all.**

Two different questions, both legitimate grounds for choosing a recipe. Filing this as "a pattern over
flow 2" would undersell what is actually interesting about it, which is a *strategic refusal*, not a
wiring detail.

> **⚠ No new codegen target.** The DUT is still `composite_kernel`, the TB still `sequential_xsi_tb`.
> `waveflow/build/codegen_targets.py` carries an explicit must-not-drift contract with that page, and
> the index already has a *"Two targets that are not flows"* section. This needs its **mirror** —
> *"a flow that is not a new target"* — stated on the page, or someone will add `peripheral_kernel` to
> `ALL_TARGETS` and the vocabulary will grow a member that names nothing.

### The trade, stated honestly

The flow's promise is *don't build XSI models for your environment.* The price is that **the
peripheral itself is the one thing you do build twice** — Python and C++ — and the two must agree to
the bit. You avoid modelling the channel twice; you cannot avoid modelling the converter twice.

Say this plainly on the page. A reader who thinks the flow eliminates dual modelling will be surprised
by the conformance work, and that surprise is avoidable.

### Page

```
docs/guide/flows/peripheral.md    third sibling of sequential.md and concurrent.md
```

Worked example: `rf_loopback`, the way `mem_copy` serves the concurrent flow and `simp_fun` the
sequential one. The RF pages then **reference** it rather than teaching it, which is what keeps the
methodology from living in two places and rotting in one — the failure mode this restructure already
found four instances of.

## What gets built

Four pieces, small, and mostly existing patterns applied one boundary over.

| piece | shape |
|---|---|
| **capture** | `Rfdc` dumps its AXIS-side words to bundles, both directions — the `RfDataSink.out_bundle` `DynParam` pattern, moved to the fabric boundary |
| **`Rfdc.rf_from_stored()`** | the replay mapping, `to_real(stored) * full_scale`, on the module that owns `full_scale` and `SampType` |
| **the round-trip test** | parametrized, see below — this is the load-bearing claim |
| **replay TB** | `RfDataSource` fed the dequantized vector; recorded output compared against the RTL capture, integer-exact |

**Capture the stored integers, not the RF-side reals.** Both would replay correctly, but the ints *are*
the interface contract — exactly what the logic sees — comparison is integer-exact with no float
question, and the dequantize back is (measurably) identity. RF-side reals would make the vector's
meaning depend on the converter configuration that produced it.

## The identity that must be a test, and the sharp bit about it

Capture does `q = from_real(x / fs, SampType)`; replay hands the converter `x' = to_real(q) * fs`, and
the converter re-quantizes `q' = from_real(x' / fs, SampType)`. So the claim is **not** the robust
quantizer round trip `from_real(to_real(q)) == q`. It is

```
from_real( (to_real(q) * fs) / fs, SampType )  ==  q
```

— which contains a float multiply and divide by the same `fs`. That pair is exactly where it could
fail, and it is why this must be a parametrized test rather than an assumption.

Measured 2026-08-16 in a shell over every stored value for `nbits` ∈ {8, 12, 16} × `full_scale` ∈
{1.0, 0.5, 0.25, 0.7, 1.3, 0.1} — **18 combinations, 0 mismatches**, including non-power-of-two
`full_scale`. A shell measurement is not a guard; promote it.

## Four gotchas the pages must state

1. **Rate does not survive a naive replay.** Push recorded words through a `StreamDriver` and you get
   a functionally-correct, rate-wrong test, *silently* — `StreamDriver` has no notion of the sample
   clock. Routing the vector back through the converter is what preserves pacing, and it is the whole
   reason the vector is dequantized rather than injected as words.
2. **Capture the stored integers, not the RF-side reals** — the reasoning above.
3. **The recorded output contains the startup transient.** Compare off by `loop_blk_latency` (2), or
   the leading zero-fill reads as a failure.
4. **`nbits` and `full_scale` must match between capture and replay**, or the round trip is not
   identity and the injected samples are not the recorded ones.

## The two RTL paths answer different questions

Worth teaching both, and worth being explicit that they are not redundant:

| path | proves | at what cost |
|---|---|---|
| converter model in XSI (today's `rtl.md`, the 1066 gate) | the **rate** property — `ADC_DROPPED == 0` under a real converter's pacing | scenario limited to what an XSI peer can generate |
| recorded replay (phase 2) | the **functional** property against an RF scenario too rich to run at RTL | says nothing about rate; the recording already fixed it |

Neither subsumes the other. A page that presents replay as "the better RTL test" would be wrong.

## Docs impact on the example

The walkthrough gains a step and the split may want revisiting:

- **`build.md`** — unchanged, plus the capture `DynParam`s where the `Rfdc` is created.
- **`run.md`** — the capture happens here; the recorded vectors are an *output* of the Python run.
- **`rtl.md`** — currently step 7. Becomes two: the converter-in-XSI run (rate) and the replay run
  (function), with the table above as the reason both exist.

Whether that is a fourth page or a second half of `rtl.md` is a judgement call to make when the prose
exists, not now.

## Gates

- Every number the new pages quote goes into `test_documented_numbers.py`, in the same commit. That
  discipline is why phase 1's page survived six months; the two claims that rotted are precisely the
  two nobody wrote a line for.
- The round-trip identity as a parametrized test — the one that makes the whole flow sound.
- `test_markdown_integrity.py` — a new `guide/flows/peripheral.md` needs inbound links from the flows
  index and from `guide/rf/python/fidelity.md`, whose closing paragraph is the natural referrer.
- `-m xsi` genuinely runs this time: unlike both phase-1 passes, this changes example code.

## Open questions

- **Does the capture machinery belong in `waveflow/` or in the example?** It starts in the example
  because that is where the reason for it lives — but so did `StreamDriver`, and it ended up
  framework. Decide deliberately when the second peripheral appears rather than discovering it.
- **Which page owns the "peripheral" vocabulary** — `guide/flows/peripheral.md` defines it, but
  `guide/flows/modules.md` owns the cut, and the definition above is a statement *about* the cut.
  Probably a link from `modules.md`, keeping one definition.
