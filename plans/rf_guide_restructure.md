# Plan: restructure `docs/guide/rf` around using it

**Status:** designed 2026-08-15. `python/quickstart.md` drafted for review; the rest not started.

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

- **Is `xsi/` about how it lowers, or how to run it?** Different readers. "How `Rfdc` becomes
  `RfdcAdcMaster`" is implementation curiosity; "how do I run this at RTL and read the counters out"
  is use-facing, and thinner than expected because the machinery is generated. If it is mostly the
  latter, `xsi/` is two pages, not four.
- **Does the wrapper belong in `xsi/`?** It is not RF-specific — `guide/comp_codegen/rtl_module.md`
  covers it. Probably a link, so RF stays about RF.
- **Naming, unrelated but adjacent:** the capture example is `examples/rf_capture/`, its class is
  `RfSampBufRx`, and its TCL is `rf_samp_buf_rx.tcl`. Three names for one thing; the docs will have to
  pick one.
