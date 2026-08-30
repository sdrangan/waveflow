# Plan: restructure `docs/guide/rf` around using it

**Status: `python/` DONE, `xsi/` NOT STARTED** — 2026-08-16, branch `rf-docs-restructure`. Stages 1–5
landed; stage 6 is deliberately unstarted pending the open question below, which is now answered with
a recommendation rather than a guess. See *What landed*.

## The problem

The five pages that exist are organised around **what we learned** — the model, how it was proven,
what it cannot do. A reader who wants to *add a converter to their design* has to reconstruct the
how-to from the narrative. `converter.md` explains how the `Rfdc` model works and how it was
validated; it never says "here is how you instantiate one."

That is a real cost and not a stylistic one: this section was written page-by-page as each result
landed, so its order is the order we discovered things in. Nobody reads in that order.

## The spine: do → understand → limits

```
docs/guide/rf/
  index.md            what a converter is, and a map of this section.  Short.
  python/
    quickstart.md     NEW — wire one up and run it.  The shortest path to samples flowing.
    converter.md      instantiating Rfdc: the parameter split, the four ports
    rf_side.md        connecting the RF side — RFSampIF, sources and sinks, t0
    axis_side.md      connecting the fabric side — packing, samp_per_word, the rate check
    sampling.md       the model you have been using: block-LT, the metronome, the sample grid
    capture.md        RfSampBuf — the capture buffer
    rules.md          NEW — the design rules
    fidelity.md       what this modelling can and cannot tell you
  xsi/
    index.md          what changes at RTL, and what it buys
    converter.md      how Rfdc lowers — bfm_model, the two models
    rf_side.md        the RF side in XSI — behavioural edges, file-backed peers, bundles
```

Two placement decisions worth their reasons:

**`sampling.md` comes after the wiring pages.** A reader adding a first `Rfdc` does not need block-LT
theory to get output on a sink; they need to know `blksize` is a knob, which the quickstart says in a
line. But `capture.md` genuinely *does* need the sample grid — its four command cases are all in
sample indices — so the model sits between them.

**`python/` ends with `fidelity.md`, which is a free hinge into `xsi/`.** That page's conclusion is
"here is what this cannot see", and the answer is the next section. A reader arrives at XSI knowing
why they are there rather than being told RTL is more accurate in the abstract.

## `rules.md` is the page that does not exist and should

The laws are currently scattered across four pages and an example. They are neither Python facts nor
XSI facts nor concepts — they are the things that make a design **wrong** if broken, and a reader
needs them before writing code. Every one was paid for in the arc:

1. **Never stall the converter's stream** — and the asymmetry: that law is the *ingress's alone*. The
   stage behind a buffer may block freely. Getting it backwards makes the capture wrong.
2. **Two tasks ≠ overlap.** A stage that consumes a whole block then emits one still has a
   non-consuming phase, whatever channel sits after it.
3. **Buffer ≥ stall × word rate.** Splitting into tasks *moves* the buffer; it never removes the need.
4. **Port capacity ≠ design capacity.** `samp_rate ≤ samp_per_word × f_axis` is what the port carries;
   divide by the consuming task's `fire_cycles` for what the design absorbs.
5. **The counters are the contract** — `underrun`, `overrun`, `dropped`, `too_old`. Assert them, and
   drive each off zero at least once, because a counter that has never counted is not evidence.
6. **Timestamp from the sample index, never from arrival time.** Arrival time is block-quantised and
   differs between backends; sample index is exact in both.
7. **Internal channel depth is physical; a boundary port's is silently discarded.**

1–4 make a design correct; 5–7 make it checkable.

## The editorial rule for the existing prose

> **A claim earns one sentence of evidence, not a story.**

"This cost 1695 of 4096 samples in a real run" is what makes a rule credible. The diagnosis belongs in
`plans/`. That keeps the repo's habit of never asserting without evidence without turning a how-to
into a lab notebook.

Concretely: `converter.md` (174 lines) splits three ways — the how-to half to `python/converter.md`,
the underflow/overflow and rate-conversion contracts to `rules.md`, the proving narrative to `plans/`.
`capture.md` keeps the four cases and the horizon; its dropped-samples diagnosis becomes one sentence.

## Stages

1. **`python/quickstart.md`** — drafted, awaiting review. Nothing else moves until its shape is agreed,
   because it sets the voice for the rest.
2. **Split `converter.md`** into `python/converter.md` + the rules it contributes.
3. **`python/rf_side.md`, `python/axis_side.md`** — new how-tos, largely extracted.
4. **Move `sampling.md`, `capture.md`, `fidelity.md`** under `python/`, trimming narrative per the
   editorial rule.
5. **`rules.md`** — assembled from the fragments the earlier stages leave behind.
6. **`xsi/`** — last, and see the open question below.

**Gate throughout:** `tests/docs/test_markdown_integrity.py` (every relative link resolves) and
`tests/docs/test_documented_numbers.py`. Moving pages breaks links; that is what the guard is for.

## Open questions

- **Is `xsi/` about how it lowers, or how to run it?** Answered below, under *The `xsi/` question*.
- **Does the wrapper belong in `xsi/`?** It is not RF-specific — `guide/comp_codegen/rtl_module.md`
  covers it. Probably a link, so RF stays about RF.
- **Naming, unrelated but adjacent:** the capture example is `examples/rf_capture/`, its class is
  `RfSampBufRx`, and its TCL is `rf_samp_buf_rx.tcl`. Three names for one thing; the docs will have to
  pick one. **Still open** — nothing in this pass touched it.

---

## What landed

```
docs/guide/rf/
  index.md            REWRITTEN — what a converter is (the three facts), the kinds table, a map
  figures/            + rf_source_sine.svg, rf_late_producer.svg
  python/
    index.md          NEW — the nav grouping page, and why the order is what it is
    quickstart.md     kept; frontmatter re-parented, links repointed, ONE CLAIM CORRECTED
    converter.md      NEW page from the how-to half of the old converter.md
    rf_side.md        NEW — largely extracted from sampling.md and the example
    axis_side.md      NEW — largely extracted from converter.md
    sampling.md       MOVED + trimmed
    capture.md        MOVED + trimmed
    rules.md          NEW — the seven rules
    fidelity.md       MOVED + trimmed, and it now ends by naming what needs RTL
```

`docs/guide/rf/converter.md` is deleted; its three parts went where the plan said.

### Which pages are new writing and which are relocations

**New prose:** `python/index.md`, `python/rules.md`, and the "what needs RTL" close on
`fidelity.md`. `python/rf_side.md` and `python/axis_side.md` are *mostly* extraction — the interface
parameter table, the source/sink fields and the constructor's refusals are new, the rest is moved.
`python/converter.md` is the old page's how-to half plus a parameter table that did not exist as such.

**Relocations with trims:** `sampling.md` lost its `t0` binding mechanics and its per-channel-skew
history (both now in `rf_side.md`, the second compressed to two sentences) and its `assert_clean`
how-to. `capture.md`'s rate-contract diagnosis went from three paragraphs to two sentences.
`fidelity.md`'s "why not just make pysim stricter" kept its table and lost the surrounding argument.

### A fourth stale claim, in the page that sets the voice

`python/quickstart.md` said *"the delay is exactly one block"* and *"the first block is flat"*,
beside a figure **it generates** that is labelled `zero-fill: 2 blocks` and `2 blocks = 512 samples`.
`loop_blk_latency` has been 2 since the ADC hop was paced honestly. The page and its own committed
measurement disagreed, in the newest page in the section.

That is four stale claims across two sections, every one of them outside
`test_documented_numbers.py` and none of them in a page anybody thought was rotting. The one in the
quickstart is the sharpest: the figure was right and the sentence next to it was wrong.

### The guard

`tests/docs/test_documented_numbers.py`: 17 → 24 tests over both commits. New on this one — rule 1's
before/after loss (repointed from the deleted `converter.md`), rule 4's `fire_cycles` and 1695/4096,
rule 6's two startup transients (both live), and the `Rfdc` parameter split checked against
`Rfdc.__annotations__` on **both** pages that state it, so the guide and the example cannot drift
from each other or from the class.

### Gates

`tests/docs` 24 passed, including the link and anchor guards over ten moved or new pages. Dev loop
`6 failed, 2704 passed, 2 skipped, 168 deselected` against a measured baseline of
`6 failed, 2697 passed` — same six pre-existing failures, +7 new guard tests. `-m xsi` not run: no
example code changed.

---

## The `xsi/` question — a recommendation, not a guess

**It should be use-facing, and that makes it two pages, not four.**

Three reasons, in the order they convinced me.

**1. The lowering is already documented, and not here.** `python/converter.md` states the two-model
split and why one declaration cannot express it. `guide/comp_codegen/xsi_tb.md` owns per-port model
resolution; `guide/custom_hooks/behavioral.md` owns the channel; `guide/comp_codegen/rtl_module.md` owns
the wrapper. A four-page `xsi/` would restate all of that under an RF heading, which is the second
copy that rots — and this pass just found four claims that rotted for exactly that reason.

**2. The reader arriving from `fidelity.md` has a question, and it is not "how".** That page now ends
by naming one clause of one contract that this backend cannot check, with a case where pysim said
zero and the hardware lost 72 words. The reader wants to *run the check*. "How `Rfdc` becomes
`RfdcAdcMaster`" does not answer that; it is interesting to someone maintaining the generator, and
that person is better served by the code.

**3. The counters are the whole payload, and they do not line up.** pysim counts blocks on the edge;
XSI counts words for an ADC drop, cycles for a DAC underrun, blocks on the channel. That mapping is
genuinely RF-specific, genuinely not written down anywhere, and genuinely what someone reading an XSI
run needs. It is also the input to the cross-backend equivalence harness
(`plans/behavioral_edges.md` S4) that does not exist yet.

So:

```
docs/guide/rf/xsi/
  index.md      what changes at RTL and what it buys: the one clause pysim cannot check,
                what to run, and the file-backed peers that make the scenario identical
  counters.md   reading the output: what each counter means, why the two backends do not
                agree, and which disagreements are physics rather than artifacts
```

`counters.md` is the page with no substitute. Both are writable from the existing gates — the
constants in `test_rf_loopback_xsi.py` are the numbers, so every figure on them can go into the guard
on the day they are written.

**What would change my mind:** if a reader is expected to *write* a new converter model rather than
use `Rfdc`, the lowering becomes a how-to and deserves its own page. Nothing in the arc suggests that
yet — `Rfdc` is the only converter, and a second one is not on the roadmap.

**⚠ Interacts with a plan written after this one.** `plans/rf_example_restructure.md` phase 2 adds
`guide/flows/peripheral.md` — the capture-replay methodology as a third flow, general rather than
RF-specific. Its scope overlaps `xsi/index.md`'s proposed *"what changes at RTL and what it buys"*.
Write `peripheral.md` first and let `xsi/index.md` be the RF-specific residue, or the same argument
lands on two pages and one of them rots. `counters.md` is unaffected — it has no substitute either way.

---

## Left for someone with authority to change code

Nine docstrings and comments in `examples/`, `waveflow/` and `tests/` point at
`docs/guide/rf/fidelity.md` or `guide/rf/sampling.md`, which are now under `python/`. They are prose
in `.py` files rather than links, so no guard sees them, and this pass was scoped to docs:

```
examples/rf_capture/rf_capture.py       (3)
examples/rf_loopback/rf_loopback.py     (1)
tests/examples/test_rf_loopback_xsi.py  (3)
tests/hw/test_rf_sample_if.py           (1)
tests/hw/test_stream_offer.py           (1)
waveflow/hw/interface.py                (1)
```
