# Planning tests

Specs for planning-only blind tests (`plans/guide_system_first.md`, Stage 0).  An agent with nothing
but the Waveflow MCP tools is given a system -- its architecture, its exact boundary, its operations
and a test plan -- and asked for a **plan** to build its Python and RTL models with Waveflow: the
modules, the interfaces and messages, and the verification, each item citing the snippet it starts
from.  No code is written and nothing is built, so a run is cheap; the plans are scored against a
reference plan per spec.

What each spec fixes, and what it leaves to the agent:

| level | in the spec |
|---|---|
| the boundary: every external stream's messages -- fields, widths, order, type tags, framing, errors | exact; the packing into words is the agent's |
| the architecture: the modules, each one's operation, what flows between them | given, in words |
| the realization: Waveflow classes, interface types, internal schemas, bodies, the verification | the agent's -- this is what is scored |

The specs form a ladder, each rung reusing the one below:

| # | spec | the system |
|---|---|---|
| 1 | [01_qam16_llr.md](01_qam16_llr.md) | one free-running kernel on streams in the shape of the `stream_kernel` example, `PF` lanes, fixed point |
| 2 | (to write) block equalizer | two input streams, two stages at fixed rates, a division |
| 3 | [03_peak_detect.md](03_peak_detect.md) | two stages at a rate that depends on the data, a wide link serialized, a tagged union |
| 3b | (later) the peak detector, architecture open | the same boundary and test plan, no stages given: does the agent find the split? |
| 4 | (to write) FFT matched-filter detector | a pipeline around the framework FFT, rung 3's detector at its end |
| 5 | (to write) packet capture | rung 4's detector, a capture kernel, a shared memory, a host |

Every spec links [deliverable.md](deliverable.md), which `waveflow blind-test` copies into the trial
folder with it.  Nothing else in this directory reaches the agent; the reference plans live in
`answers/`, which no spec links.

```bash
waveflow blind-test --prompt evaluation/planning_tests/01_qam16_llr.md
```

This directory is outside `docs/` and `examples/`, so the knowledge index never serves it to an agent.
