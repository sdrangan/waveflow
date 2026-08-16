# Plan: `docs/examples/rf_loopback` — restructure as a walkthrough

**Status:** audited 2026-08-16, not started. Follows `plans/rf_guide_restructure.md`, which cites this
example as its source of truth — so this lands first.

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

## Open question

Does the guide's quickstart cite `build.md` or `index.md`? It currently says *"the full walkthrough has
every line"* and links `python.md`. Pointing at `index.md` survives further splits; pointing at
`build.md` lands the reader where the code is. Probably `index.md`, with the sub-pages listed there.
