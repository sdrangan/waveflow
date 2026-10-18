---
title: Composing a parent
parent: Developing module by module
grand_parent: Modules
nav_order: 5
audience: python
api: [add_system_steps, ModuleStore, HwSystem]
summary: "Calibrate each unit's timed phases in its harness, then compose a system from them. A parent's pysim is composed from its units: each child loads the record its own harness filed, and nothing is fitted to the parent. The check is the parent's RTL -- the polynomial on a bus +1.8%, markov (a generator, a chain, a memory, on a bus) -0.9% (Vitis 2025.1). When a composition misses, the miss locates what is not yet modeled: markov's first was -8.8%, and comparing RTL probes against pysim found an uncalibrated memory, then a lead the chain's core needed. A parent's pysim runs after its units' calibrations (add_system_steps(..., after=[...]) or dag.require). Builds are incremental: change one unit and only it, and the systems holding it, do any work."
---

# Composing a parent

## Compose a system from calibrated units

Once each unit has been verified and its timed phases calibrated in its [harness](units_harness.md), a
parent's pysim is **composed**: each child loads its own record
([Calibrating in a harness](units_calibration.md)), and nothing is fitted to the parent. The check is
the parent's RTL:

| system | composed from | against its RTL (Vitis 2025.1) |
|---|---|---|
| [the polynomial on a bus](../../examples/stream_kernel_mm/composition.md) | the kernel's record, the queues, the crossbar | +1.8% |
| markov: a generator, a chain, a memory, on a bus | three records, each from its own harness | -0.9% |

When the composition misses, the miss locates what is not yet modeled: markov's first composition was
-8.8%, and comparing its RTL probes against pysim event by event found an uncalibrated memory (reads at
two cycles a word, one transaction at a time), then a lead the chain's core needs from its input. Both
became calibrated units; nothing was fitted to the system ([markov's timing](../../examples/markov/pysim.md)).

## Order

A parent's pysim must run after its units' calibrations, so its estimate is made from the records they
just filed: `add_system_steps(..., after=[...])`, or `dag.require("pysim", [...])` when the parent was
added first -- markov's build collects each unit's `phase_calib` and `mem_timing` artifact and requires
them of the system's `pysim` ([`markov_build.py`](../../../examples/markov/markov_build.py)).

## Incremental

Every system step is fresh while what it reads is unchanged
([freshness](../build/xsi_system.md#system-freshness)): structure, sources, scenario, the RTL of its
tops, and -- for pysim -- the records its modules load, each module's own. So a unit nobody changed does
no work. Change markov's generator's draw interval (`MarkovGen.gen_ii`, a build parameter) and only the
generator is re-synthesized, re-run and calibrated, with the system that holds it; the chain's and the
memory's harnesses skip ([`test_incremental_units.py`](../../../tests/examples/test_incremental_units.py)).
