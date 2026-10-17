---
title: Developing module by module
parent: Hardware modules and Flows
nav_order: 3.5
audience: python
api: [HwSystem, harness, memory_harness, StreamDriver, StreamSink, CreditSink, CreditSource, MmScript, StandInPolicy, add_robustness_steps, TimedPhase, add_calib_steps, MemCalibStep, ModuleStore]
summary: "How a design of several modules is developed one module at a time. Any module in a design's tree can be run alone: harness(module) makes it the DUT of its own HwSystem, with a stand-in at each port generated from the port's type (a stream driver or sink, a credit source or sink, a scripted AXI-MM master), each with a C++ twin so the harness runs in pysim and at RTL. Stand-ins can be made hostile (StandInPolicy: stalls, late answers, starved credit) and a harness swept over them. A module's timing is declared as timed phases and calibrated in its harness; the fit is filed under the module's identity and loaded wherever the module sits. A parent's timing is then composed from its units' records with nothing fitted, and checked against the parent's RTL -- markov within 1.6%. Builds are incremental: change one unit and only it, and the systems holding it, do any work."
---

# Developing module by module

A design is a tree of modules -- kernels, memories, the devices that put kernels on a bus, the systems
that hold them. Developing it module by module means: run each module **alone**, verify it, measure
its timing, and then **compose** -- a parent's behavior and timing follow from its children's, checked
once against the parent's RTL. This page is the practice; [Free-running polynomial](../../examples/stream_kernel/index.md)
is it on one kernel, and [Two kernels on a bus](../../examples/markov/index.md) on a system of several.

## A unit is any module in the tree

There is no unit class. Any `HwModule` in a design's DUT tree can be built and run alone -- a leaf
kernel, a memory, a composite of several. The unit tree **is** the module tree: a module's children,
boundary and wiring are already declared by the module itself.

What runs is always an [`HwSystem`](concurrent_system.md): a simulation plus **one named DUT**. The
testbench is everything else in the simulation, derived, never listed. Each testbench participant must
have a lowering to the RTL testbench (its `bfm_model()`), so the same system runs in pysim and under XSI.

## The harness: a module made the DUT

```python
sysm = harness(PolyKernel, workload="test")      # or harness(some_instance)
```

[`harness`](../../../waveflow/hw/harness.py) builds a fresh copy of the module (its class and its build
parameters) as the DUT and puts a **stand-in** at each port, chosen by the port's type:

| the module's port | stand-in (pysim) | C++ twin (XSI) |
|---|---|---|
| a stream input | `StreamDriver`, playing a bundle | `AxisMaster` |
| a stream output | `StreamSink`, recording | `AxisSlave` |
| a credit stream it produces into | `CreditSink`: takes everything, reports credit | `CreditSink` |
| a credit stream it consumes | `CreditSource`: plays bursts when the window has room | `CreditSource` |

A memory's slave port is harnessed by `memory_harness(make, script)`: the memory behind a direct link,
driven by `MmScript`, a scripted AXI-MM master that records when each read and write ran.

The module supplies its scenarios, `workload(kind)` -> `{input port: bursts}`, so a harness needs no
testbench code. A credit port's window is compiled into the producer's RTL, so the module states it
(`credit_links`) and the harness refuses to guess.

A harness's DUT is a root, so `add_system_steps(dag, harness(...), ...)` runs it through the whole
[system flow](../build/xsi_system.md): csynth, the RTL run, and the comparison of the stand-ins'
recordings, RTL against pysim.

## Hostile stand-ins

A polite testbench never shows that a module survives anything else. A
[`StandInPolicy`](../../../waveflow/simulation/policy.py) makes each stand-in misbehave as its protocol
allows: a sink drops TREADY, a driver holds off its next word (never withdrawing one it presented), a
credit sink holds its reports (starved credit), a bus master takes its answers late.

```python
add_robustness_steps(dag, lambda p: harness(MarkovGen, policy=p), work_dir=..., top=..., prefix="rob_")
```

Each policy that touches a harness's stand-ins adds it again as a system of its own; its `compare` is
the check -- the RTL must still finish with exactly pysim's words. A policy that changes nothing finds
nothing, so the sweep is harsh: 90% stalls, credit held 2000 cycles.

## Calibrating a leaf

A module's timing is declared as [timed phases](../timing_model/timed_phases.md): spans between
handshakes at its own ports, with the features their durations depend on. Three patterns cover the
bodies seen so far: a sequential wait (`timeout`), a pipelined load-compute-store (`pipeline`), and a
write paced by its producer (`produce`), which may wait on several earlier events and start when all
of them allow.

```python
calib_holdout: ClassVar[dict] = {"nwords": (48, 128)}           # values that test the fit
def workload(self, kind="test"): ...                               # "calib": the calibration scenario

add_calib_steps(dag, harness(PolyKernel), work_dir=..., top=..., store=ModuleStore(HERE / "calib"))
```

`add_calib_steps` runs the module's calibration scenario in its harness, traced, and fits each span
from the RTL's times and pysim's pairing ([Fitting the model](../timing_model/fit.md)). The fit is
**filed under the module's identity** -- a digest of its structure, free of what its ports are bound to
-- together with the phases it fits. Wherever the module sits, its phases load the record its identity
finds; a repeat finds it and runs nothing; a module whose structure or phases changed finds none and is
calibrated again. A memory is calibrated the same way from `memory_harness` (`MemCalibStep`).

## Composing and verifying a parent

A parent's pysim is then **composed**: each child loads its own record, and nothing is fitted to the
parent. The check is the parent's RTL:

| system | composed from | against its RTL (Vitis 2025.1) |
|---|---|---|
| the polynomial behind AXI-MM | the kernel's record, the queues, the crossbar | +1.8% |
| markov: a generator, a chain, a memory, on a bus | three records, each from its own harness | -1.6% |

When the composition misses, the miss locates what is not yet modeled: markov's first composition was
-8.8%, and comparing its RTL probes against pysim event by event found an uncalibrated memory (reads at
two cycles a word, one transaction at a time), then a lead the chain's core needs from its input. Both
became calibrated units; nothing was fitted to the system.

A parent's pysim must run after its units' calibrations, so its estimate is made from the records they
just filed: `add_system_steps(..., after=[...])`, or `dag.require("pysim", [...])` when the parent was
added first.

## Incremental

Every system step is fresh while what it reads is unchanged
([freshness](../build/xsi_system.md#system-freshness)): structure, sources, scenario, the RTL of its
tops, and -- for pysim -- the records its modules load, each module's own. So a unit nobody changed does
no work. Change markov's generator's draw interval (`MarkovGen.gen_ii`, a build parameter) and only the
generator is re-synthesized, re-run and calibrated, with the system that holds it; the chain's and the
memory's harnesses skip ([`test_incremental_units.py`](../../../tests/examples/test_incremental_units.py)).
