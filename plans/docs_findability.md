# Plan: making the docs findable

**Status: SCOPE, not started.** 2026-08-21. Short-term measures only; the long-term answer is a
headless MCP docs server and has its own plan.

## The problem, with two instances from one session

The docs are good and the machinery is documented. **It is not findable at the moment of need**, and
the failure is specific: a reader has a *question*, the docs are organised by *topic*, and the two
do not map.

Both of these were rediscovered by measurement in a single session, by two different readers:

| the question | where the answer actually lives | what happened |
|---|---|---|
| *"what does a `DataArray` of a numpy-backed field hold?"* | `guide/vectorization/python/numerical.md:19-39` — clearly, with an example | grepped `dataschema.py`, found nothing, wrote a probe and measured it |
| *"can a word be wider than 64 bits?"* | `guide/interface/overview.md:61` and `guide/schema/hls/serialization.md:86` | grepped the source, found the `Words` alias, measured it. `vectorization/python/integer.md:84` says *"Wide (> 64-bit) support is future work"* — correct about **arithmetic**, and the page a reader asks the **storage** question on |

**The rule those two obscure between them**, which is worth stating once somewhere prominent:

> **Storage: arbitrary bitwidth. Arithmetic: results capped at 64 bits.**

Two different axes, both limits deliberate, and no page currently says them side by side.

## Why the readers went to the source

Not laziness — **calibration**. This session found four stale doc claims (`rf_loopback`'s "zero
dropped", the quickstart's "delay is one block", the `iq_mode` convention, `capture.md` describing a
retired design). A reader who has been burned that way treats source as authoritative and docs as a
hint.

So a fix that requires the reader to *go to the docs first* does not address the observed behaviour.
It has to meet them where they already are.

## Fix 1 — source-to-docs pointers on the classes people reach for

`DataArray`'s docstring is one line — *"Class-driven array schema with fixed maximum shape and
optional dynamic extent."* No mention that `.val` is an `ndarray`, no pointer to the page that says
so.

Meanwhile the RF modules are dense with `see docs/guide/rf/...`, **which is exactly why RF docs keep
being found and nothing else does.** That asymmetry is the whole finding.

A `See: <page>` line on the ten or so most-reached-for classes — `DataArray`, `IntField`,
`FixedField`, `StreamIF`, `Words`, `DataList`, `MemAddr` — would have caught both misses above,
because in both cases the reader was *already in the source*.

Cheap, and it also signals which pages are maintained.

## Fix 2 — a generated summary index

**156 of 255 pages already carry a `summary:` in frontmatter**, written to be informative, and
nothing consumes them.

Generate one page — every guide page's title, path and summary — as a build step. Then *"grep the
guide"* is one file rather than a hundred, over text already dense with the vocabulary someone would
search for.

The property that matters: **it cannot rot.** It is derived from frontmatter, so it is current by
construction, and a page with no summary shows up as a hole rather than disappearing. Contrast a
hand-written FAQ, which is stale within a month — this repo has found four such staleness bugs in a
week.

`tests/docs/test_markdown_integrity.py` already parses frontmatter, so the reader exists.

## Why these two and not a third

They fix **different paths**, and today's misses were both the second:

- Fix 2 helps a reader who thinks *"the docs must cover this somewhere"*.
- Fix 1 helps a reader who is already in the code and will not leave it.

A glossary, a hand-written FAQ, or a question index all rot, and rot is the failure mode this repo
keeps paying for.

## The down-payment argument

A generated summary index is **also the natural corpus for the headless MCP docs server**. Rather
than a stopgap to be discarded, it is the artifact that server would serve — so the short-term fix
is a first instalment on the long-term one rather than a detour.

## Two small corrections worth making at the same time

- **`vectorization/python/integer.md:84`** — the sentence is correct about arithmetic but silent on
  storage, on the page where the storage question gets asked. One parenthetical and a cross-link.
- **`DataArray`'s docstring** — state that `.val` is the underlying `ndarray`, and link the page.

## Open

- **Where does the generated index live?** `docs/guide/all.md` is the obvious spot, but it must not
  appear in the nav as a page anyone is expected to *read* — it is a grep target.
- **Which classes get pointers?** Ten is a guess. The honest way to pick is to look at what actually
  gets reached for, which the session transcripts show better than intuition.
- **Does the summary index need the API list too?** Many pages carry an `api:` field naming the
  symbols they document. Including it would make *symbol → page* greppable, which is the exact lookup
  both of this session's misses needed.
