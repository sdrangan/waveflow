---
title: Timing-model figure sources
nav_exclude: true
search_exclude: true
summary: "The timing-model figures follow the guide's convention: generated from a source committed in the repository, never hand-drawn, so a figure cannot drift from what it shows."
---

# Timing-model figures — source → committed artifact

Same convention as [`docs/guide/flows/figures/`](../../flows/figures/README.md): the output is
**generated** from a committed source, never hand-drawn.

| Output | Source | Regenerate with | Shows |
|---|---|---|---|
| `timed_phase.svg` | *a run*: poly's traced calibration cosim, `examples/stream_inband/vcd/calib_w32.vcd` | `python examples/stream_inband/poly_build.py --through sync_docs_figures` | one poly command beat by beat on `s_in` and `m_out`, and the three spans its phases declare, measured between the events that bound them |
