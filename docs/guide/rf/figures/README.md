---
title: RF figure sources
nav_exclude: true
search_exclude: true
---

# RF figures — source → committed artifact

Same convention as [`docs/guide/flows/figures/`](../../flows/figures/README.md): the source is
committed beside its output, and the output is **generated**, never hand-drawn, so a diagram cannot
drift from what it describes.

| Output | Source | Regenerate with | Shows |
|---|---|---|---|
| `rf_domains.svg` | `rf_domains.tex` | `bash render.sh` | the two domains an RF design spans, with the converter on the boundary |
| `rf_loopback_sine.svg` | *a run* | `python examples/rf_loopback/rf_loopback_figures.py` | a windowed sinusoid through the loopback: the one-block shift and the leading zero-fill |

Two different kinds of source, deliberately:

- **`rf_domains`** is a **drawing** — TikZ, rendered by `render.sh` (MiKTeX pdflatex → dvisvgm). It
  asserts a structural claim, so its source is a diagram.
- **`rf_loopback_sine`** is a **measurement** — matplotlib, rendered from an actual simulation run.
  It asserts what the model *does*, so its source is the model. Re-run it after changing the example
  and commit the result; a stale plot is a claim nothing checks.
