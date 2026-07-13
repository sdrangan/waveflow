---
title: The harness for AI
parent: Overview
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
  **local**: an agent fills in one component against an explicit interface *contract*, not a
  monolithic kernel it has to get entirely right at once. This is what lets AI scale past local
  fragments to whole systems.
- **Deterministic, reproducible builds** — the build graph runs the same way every time, so a
  generated design can be rebuilt, compared, and trusted.
- **Built-in, bit-exact validation** — every result is checkable against the real toolchain, so
  the output is **verified**, not just plausible.

Waveflow is that substrate.

## AI is downstream, not the center

AI is a **first-class downstream consumer** of the representation — a real strength, and an active
area of development — but it is grounded *by* the substrate, not the center of it. The codegen
pipeline itself is deterministic (structured `hwgen`, not an LLM), and Waveflow is the substrate
**beneath** an agent, not just an orchestration layer **over** one. That is the difference between
AI output that is merely plausible and AI output you can trust.

## AI in Waveflow (future work)

Waveflow is designed to integrate AI in three places — an active and largely forward-looking area
of development:

- **AI-assisted codegen.** Generate a component's HLS kernel from its Python model against the
  typed interface contract, so an agent fills in one well-scoped block at a time instead of a whole
  design. (Today the codegen pipeline is deterministic `hwgen`; AI assistance layers on top of it.)
- **Agentic design-space exploration.** An agent drives the fast inner loop, using tools that
  extract performance, timing, and resource estimates from the SimPy simulation to choose the next
  design point and iterate.
- **MCP tooling.** Model Context Protocol servers help users write Waveflow code and let AI agents
  query simulation and synthesis results directly — grounding suggestions in real artifacts rather
  than guesswork.

These map onto [the Waveflow flow](./flow.md): AI assists at **codegen** and drives the
**agentic DSE** loop, both grounded by the bit-exact substrate.
