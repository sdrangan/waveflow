---
title: Motivation
parent: Overview
grand_parent: Waveflow
nav_order: 1
---

# Motivation

## The problem

Designing hardware for large, complex systems is enormously challenging: it requires keeping one coherent
specification aligned across algorithm, architecture, simulation, and software. Every design change ripples
through all of those layers.

Two forces now strain that flow even further. **Reconfigurability** — chips are increasingly expected to run
a widening range of tasks on shared, programmable compute, so timing and resource contention become *dynamic*
and *data-dependent*; the static analysis that worked for a fixed pipeline no longer answers the question, and
the only way to see the behavior is to **simulate** it — fast, bit-exact, *and* timing-aware. And **AI**,
which could cut that effort dramatically, but only with a structured substrate to compose against.

Both point to the same missing piece — a single executable model that is fast, bit-exact, timing-aware, and
structured. Five gaps in today's flow stand in the way:

- **AI doesn't scale to whole systems.** Large language models are excellent at generating small, local code
  fragments, but they degrade on large projects that demand consistent structure, architecture, and
  cross-cutting interfaces. Without a structured substrate to anchor them, their output doesn't compose into a
  coherent design.

- **Performance exploration falls into a gap.** Teams already build fast, *bit-exact* models — but those are typically
  *functional* models: they carry no notion of timing or resource cost, and are often scoped to a limited sub-system. The model that *does* capture timing is RTL, far too slow to sweep architecture — bit widths,
  buffering, memory organization, scheduling. So early performance questions land in a gap: the fast model
  can't answer them, and the timing-accurate model is too slow to explore.

- **The system is fragmented.** The description is spread across algorithm notebooks, architecture
  spreadsheets, simulation harnesses, ad-hoc interface code, HLS or RTL, software bindings, tests, and build
  scripts. The algorithm and hardware teams typically keep *separate* simulations and exchange test vectors by
  hand — and every design change has to be re-translated across these layers, where much of the original
  intent is lost.

- **Iteration is hard to trace — especially with AI in the loop.** A deterministic build is reproducible, but
  when the design is fragmented across separate models, a timing or resource number is hard to trace back to
  the specific design choice that produced it. AI-assisted iteration sharpens this: a variant that worked once
  is hard to reproduce and compare against the next.

- **Verification is rigorous but manual and fragmented.** Checking generated or hand-written hardware against
  the algorithm model bit-for-bit is standard practice — but today it is painstaking: the algorithm and
  hardware teams maintain separate models and exchange vectors by hand, and the harness is rebuilt per module.
  Because the golden and the hardware are not derived from one source, keeping them aligned is ongoing manual
  work. (For AI-generated code specifically, an *automatic* check is what separates *plausible* from *verified*.)

These problems compound: fragmentation makes tracing and verification manual, the functional/timing split
discourages early exploration, and the lack of structure is exactly what keeps AI from scaling past local
fragments.

## Waveflow's approach: a single, executable source of truth

Waveflow attacks these together. It describes the key elements of a hardware system — **data
schemas, interfaces, components, behavior, and build relationships** — as structured Python, so
that simulation, downstream implementation, software integration, and tooling all stay aligned
around one executable model. That one move addresses each problem directly:

- **structure for AI** — typed schemas and explicit interfaces let generation stay *local* and
  contract-guided, so an agent fills in one well-bounded component at a time instead of a whole
  system at once;
- **fast, bit-exact simulation with timing** — event-level, vectorized models run orders of magnitude
  faster than RTL while staying value-exact, *and* carry calibrated timing/resource estimates — at
  system scale, where static analysis breaks down;
- **one source, no drift** — the model *is* the system; the golden and the hardware are derived from it
  together, so there is no second copy to keep in sync;
- **deterministic, traceable builds** — a `BuildDag` makes every `gen → simulate → synthesize → verify`
  run explicit and repeatable, so a timing or resource number traces back to the design that produced it;
- **automatic bit-exact verification** — generated hardware is checked against the Python golden,
  bit-for-bit, on the real toolchain — co-located with the model, not a separate hand-built harness.

The core thesis is that for many systems — especially domain-specific accelerators — the hardest
problem is **not generating RTL**. It is keeping a coherent, executable specification across
algorithms, architecture, interfaces, simulation, software, implementation, and documentation.
Waveflow is aimed at that broader problem.

## Who it is for

- **Hardware architects** exploring system structure and interfaces before RTL lock-in.
- **Researchers in wireless, DSP, and ML** studying algorithm–hardware co-design — a more
  realistic model than a notebook, a faster workflow than RTL-first.
- **Accelerator teams** who need architecture, simulation, software interfaces, and
  implementation flows to stay aligned.
- **Tool builders** who want a structured, machine-readable hardware representation for automation
  and AI-assisted workflows.

Next: [the harness for AI](./aiharness.md).
