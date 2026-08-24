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
| `rf_interfaces.svg` | `rf_interfaces.tex` | `bash render.sh` | the four endpoints and two clock domains of one `Rfdc` |
| `rf_channels.svg` | `rf_channels.tex` | `bash render.sh` | how those endpoints multiply as channels are added: `n_rx`+`n_tx` separate AXIS ports, one `RFSampIF` per direction |
| `rf_source_sine.svg` | *a run* | `python examples/rf_loopback/rf_loopback_figures.py` | what the source plays, read back out of its bundle: a four-block windowed sinusoid |
| `rf_loopback_sine.svg` | *a run* | *(same script)* | that sinusoid through the loopback: the two-block shift and the leading zero-fill |
| `rf_late_producer.svg` | *a run* | *(same script)* | the late-producer fault: two leading flat blocks at the sink becomes four |

Two different kinds of source, deliberately:

- **`rf_domains`, `rf_interfaces` and `rf_channels`** are **drawings** — TikZ, rendered by
  `render.sh` (MiKTeX pdflatex → dvisvgm). They assert structural claims, so their source is a
  diagram. They share a layout on purpose and claim different things: `rf_domains` is about the two
  *representations* (sample blocks vs packed words); `rf_interfaces` is about the four *endpoints* of
  a one-channel converter; `rf_channels` is about the **asymmetry** those endpoints have as channels
  are added — one AXIS port per channel, but one `RFSampIF` per direction however many there are.
- **The other three** are **measurements** — matplotlib, rendered from actual simulation runs. They
  assert what the model *does*, so their source is the model. One script writes all three; re-run it
  after changing the example and commit the result, because a stale plot is a claim nothing checks.

The script pins `svg.hashsalt`, so a regeneration that changes nothing produces a byte-identical
file and a regeneration that changes something produces a reviewable diff.
