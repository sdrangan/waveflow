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
| 1b | [01b_qam16_llr_bus.md](01b_qam16_llr_bus.md) | rung 1's kernel behind AXI-MM, a two-thread host |
| 2 | [02_hamming_decode.md](02_hamming_decode.md) | a two-stage pipeline (16 correlations, then a 16-way select), the algorithm given |
| 3 | [03_peak_detect.md](03_peak_detect.md) | two stages at a rate that depends on the data, a wide link serialized, a tagged union |
| 3b | (later) the peak detector, architecture open | the same boundary and test plan, no stages given: does the agent find the split? |
| 4 | (to write) FFT matched-filter detector | a pipeline around the framework FFT, rung 3's detector at its end |
| 5 | [05_packet_capture.md](05_packet_capture.md) | two kernels, a routed link with credit, a shared memory, a host on interrupts (markov's shape) |

Every spec links [deliverable.md](deliverable.md), which `waveflow blind-test` copies into the trial
folder with it.  Nothing else in this directory reaches the agent; the reference plans live in
`answers/`, which no spec links.

```bash
waveflow blind-test --prompt evaluation/planning_tests/01_qam16_llr.md
```

This directory is outside `docs/` and `examples/`, so the knowledge index never serves it to an agent.

`retired/` holds specs that became worked examples -- `02_block_eq` is the equalizer example's spec -- and so no longer test anything: an agent could copy the example.
