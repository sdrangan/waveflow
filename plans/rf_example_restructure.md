# Plan: `docs/examples/rf_loopback` — restructure as a walkthrough

**Status: DONE, 2026-08-16** (branch `rf-docs-restructure`). See *What landed* at the foot of this
file — including **two stale claims the audit below missed**, which is the more interesting result.

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
