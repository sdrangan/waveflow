---
title: The harness for AI
parent: Overview
grand_parent: Waveflow
nav_order: 5
---

# Waveflow: the harness for AI

**Waveflow is the harness that makes AI effective for hardware design.**

AI agents can generate HLS and drive design-space exploration — but they're only as good as the
substrate they work on, and raw HDL/HLS gives them none of what they need. Waveflow gives them
four:

- **Fast simulation** — vectorized, orders of magnitude faster than RTL, so an agent can try many
  designs *inside its loop* instead of waiting on one toolchain run.
- **Structured architecture** — typed schemas and well-defined interfaces make code generation
  **local**: an agent fills in one module against an explicit interface *contract*, not a
  monolithic kernel it has to get entirely right at once. This is what lets AI scale past local
  fragments to whole systems.
- **Deterministic, reproducible builds** — the build graph runs the same way every time, so a
  generated design can be rebuilt, compared, and trusted.
- **Built-in, bit-exact validation** — every result is checkable against the real toolchain, so
  the output is **verified**, not just plausible.

Waveflow is that substrate.

## AI is downstream, not the center

AI is a **first-class downstream consumer** of the representation — a real strength, and an active
area of development — but it is grounded *by* the substrate, not the center of it. The code generators
themselves are deterministic (the structs, the tops, the testbenches and the `hwgen` body extractor, not
an LLM), and Waveflow is the substrate **beneath** an agent, not just an orchestration layer **over**
one. That is the difference between AI output that is merely plausible and AI output you can trust.

## AI in Waveflow

Waveflow integrates AI in three places — an active area of development, partly built:

- **AI-assisted design.** An agent writes one module at a time — its Python model and its HLS body —
  against the generated message structs and ports, with the golden and the RTL comparison to check it
  ([Defining the kernel in Python and HLS](../guide/systems/hwmodule.md)). **Built, experimental**: the
  [MCP server](../guide/ai_tooling/index.md) gives an assistant the guide, the reference examples and
  [frames](../guide/ai_tooling/frames.md) — a process per system shape — and
  [blind tests](../guide/ai_tooling/blind.md) run a fresh agent from a spec to a verified build.
- **Agentic design-space exploration.** An agent drives the fast inner loop, using the calibrated timing
  and resource models to choose the next design point and iterate. **Not yet a framework tool**: sweeps
  and the models are built, the exploration loop is not
  ([Design-space exploration](../guide/systems/dse.md)).
- **MCP tooling.** The [MCP server](../guide/ai_tooling/index.md) also searches the guide and examples
  locally ([search](../guide/ai_tooling/search.md)) and validates schemas; letting an agent query
  simulation and synthesis results directly is still being built.

These map onto [the Waveflow flow](./flow.md): AI assists at **codegen** and drives the
**agentic DSE** loop, both grounded by the bit-exact substrate.
