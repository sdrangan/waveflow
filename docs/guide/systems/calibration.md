---
title: Timing and resources
parent: Flow Overview
nav_order: 7
audience: python
api: [TimedPhase, add_calib_steps, ModuleStore, MemCalibStep, CsynthParser]
summary: "The fifth step of the flow: how long the design takes and what it costs, measured per module and composed. A module's timing is declared as timed phases -- spans between handshakes at its own ports, with the features they depend on -- and calibrated in its harness against its RTL (add_calib_steps), the fit filed under the module's identity and loaded wherever the module sits. A parent's timing is then composed from its modules' records with nothing fitted to it, and checked once against the parent's RTL: the polynomial behind AXI-MM within 2%, markov within 1%. When a composition misses, the miss names what is not yet modeled. Resources: each top's csynth report, and the system's from Vivado."
---

# Timing and resources

A design that works must also be fast enough and fit. Waveflow measures both **per module** and
**composes** them: each module's timing is calibrated against its own RTL, and a system's timing follows
from its modules' with nothing fitted to the system.

## Timing is declared

A module's pysim timing is a set of **[timed phases](../timing_model/timed_phases.md)**: spans between
handshakes at its own ports, each with the features its duration depends on. The polynomial declares two:

```python
self.samp_phase = TimedPhase(
    "samples", spans={"latency": (self.s_in.first, self.m_out.first),
                      "fill_time": (self.m_out.first, self.m_out.last)},
    features=("nwords",), calib_dir=HERE / "calib")
self.status_phase = TimedPhase(
    "status", spans={"status_time": (self.m_out.last, self.m_status.first)}, calib_dir=HERE / "calib")
```

- **samples** is pipelined: `latency` from the first sample word in to the first result out, then
  `fill_time` over the results;
- **status** is sequential: from the last result to the status.

Every event is a handshake at a port, so every span can be measured at RTL. Three patterns cover the
bodies seen so far: a sequential wait (`timeout`), a pipelined load-compute-store (`pipeline`), and a
write paced by its producer (`produce`).

## Calibrated in the harness

The module names a calibration scenario and the feature values to hold out of the fit:

```python
calib_holdout: ClassVar[dict] = {"nwords": (48, 128)}           # interior lengths test the fit
def workload(self, kind="test"): ...                             # "calib": nine lengths, 8 to 200

from waveflow.build.phase_calib import add_calib_steps
from waveflow.calib.record_store import ModuleStore
from waveflow.hw.harness import harness

add_calib_steps(dag, harness(PolyKernel), work_dir=..., top=..., store=ModuleStore(HERE / "calib"))
```

`add_calib_steps` runs the calibration scenario in the module's harness, traced at RTL, and fits each
span: pysim pairs the events and supplies the features, the RTL trace supplies the times
([Fitting the model](../timing_model/fit.md)). The polynomial's (Vitis 2025.1):

| span | fit | residual |
|---|---|---|
| `latency` | 29 cycles | 0 |
| `fill_time` | `nwords - 1` cycles | 0 |
| `status_time` | 1 cycle | 0 |

The fit is **filed under the module's identity** -- a digest of its structure, free of what its ports are
bound to. Wherever the module sits, its phases load the record its identity finds; a repeat finds it and
runs nothing; a module whose structure changed finds none and is calibrated again. A memory is calibrated
the same way, from `memory_harness` (`MemCalibStep`).

## Composed, and checked once

A parent's pysim timing is **composed**: each module loads its own record, and nothing is fitted to the
parent. The parent's RTL is the check:

| system | composed from | pysim against its RTL (Vitis 2025.1) |
|---|---|---|
| the polynomial behind AXI-MM | the kernel's record, the queue views, the crossbar | +1.8% |
| markov: a generator, a chain, a memory, on a bus | three records, each from its own harness | -0.9% |

**When a composition misses, the miss names what is not yet modeled.** markov's first composition was
-8.8%; comparing its RTL timing probes against pysim, event by event, found an uncalibrated memory, then a
lead the chain needed from its input. Both became calibrated modules; nothing was fitted to the system.
A parent's pysim runs after its modules' calibrations, so its estimate uses the records they just filed
(`add_system_steps(..., after=[...])`).

## Resources

Each HLS top's csynth report gives its resources -- LUTs, FFs, DSPs, BRAM -- and its estimated clock
([Reading the report](../resource/parser.md)). A system's are measured from Vivado, globally or per
device and then linked ([System resources](../resource/systems.md)). Like timing, a resource number
belongs to a tool version and a part: name both beside it.

## Steps up

**markov** composes three calibrated modules and an AXI crossbar, and its gates check the composed pysim
against the system's RTL ([markov's timing](../../examples/markov/pysim.md)).

Next: [Design-space exploration](dse.md).
