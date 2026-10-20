---
title: Calibrating in a harness
parent: Developing module by module
grand_parent: Modules
nav_order: 4
audience: python
api: [TimedPhase, add_calib_steps, calibration_workload, CalibWorkload, ModuleStore, MemCalibStep, memory_harness, harness]
summary: "A module's timing is declared as timed phases -- spans between handshakes at its own ports, with the features their durations depend on -- and calibrated in its harness. A module that declares timed phases and a calib_holdout gets a calibration workload for free: the harness of the same module on its workload('calib'), with the held-out feature values. add_calib_steps runs it traced at RTL and fits each span from the RTL's times and pysim's pairing, filing the fit in a ModuleStore under the module's identity (a digest of its structure, free of what its ports are bound to). A repeat finds the record and runs nothing; a changed module finds none and is calibrated again. A memory is calibrated from memory_harness with MemCalibStep."
---

# Calibrating in a harness

A module's pysim timing is declared as [timed phases](../timing_model/timed_phases.md): spans between
handshakes at its own ports, with the features their durations depend on. Three patterns cover the
bodies seen so far: a sequential wait (`timeout`), a pipelined load-compute-store (`pipeline`), and a
write paced by its producer (`produce`), which may wait on several earlier events and start when all of
them allow. Every event is a handshake at a port, so every span can be measured at RTL -- in the
module's own [harness](units_harness.md).

## The calibration workload

A module that declares timed phases and the feature values to hold out of the fit gets a calibration
workload from its harness, with nothing more written:

```python
calib_holdout: ClassVar[dict] = {"nwords": (48, 128)}           # values that test the fit
def workload(self, kind="test"): ...                               # "calib": the calibration scenario
```

`harness(module).calibration_workload()` is a
[`CalibWorkload`](../../../waveflow/build/phase_calib.py): the harness of the same module on
`workload("calib")`, always polite, and the held-out values. A module with no `calib_holdout`, or with
no timed phases on itself or on any task inside it, has none (`None`). The phases may be a task's
inside a composite kernel: the systolic engine's core carries a `panel` phase, whose events are the
acquire and release of a block on the engine's internal `b` channel, and the engine's harness times it
([Systolic engine](../dsp/systolic/systolic_engine.md#architecture); seeded until it is calibrated).

`calib_phases` fits every phase the workload exercised. A phase with no occurrence at all is skipped
and keeps its seed; one that occurred but has an occurrence the RTL lacks fails the step.

## Fitting and filing

```python
from waveflow.build.phase_calib import add_calib_steps
from waveflow.calib.record_store import ModuleStore

add_calib_steps(dag, harness(PolyKernel), work_dir=..., top=..., store=ModuleStore(HERE / "calib"))
```

[`add_calib_steps`](../../../waveflow/build/phase_calib.py) adds the calibration workload as a second
system (prefix `calib_`), traced at RTL, and a `phases` step that fits each span: pysim pairs the events
and supplies the features, the RTL trace supplies the times ([Fitting the model](../timing_model/fit.md)).
Add it after the module's own system steps, so `codegen` and `csynth` are shared. The polynomial's fit
is in [Timing and resources](../systems/calibration.md#calibrated-in-the-harness).

The fit is **filed under the module's identity** -- a digest of its structure, free of what its ports are
bound to -- together with the phases it fits. Wherever the module sits, its phases load the record its
identity finds. The store is asked first: a repeat finds the record and runs nothing; a module whose
structure or phases changed finds none and is calibrated again.

## A memory

A memory is calibrated the same way, from its own harness: `memory_harness` on a memory that loads no
timing, its script's op timings at RTL and in pysim, fitted by
[`MemCalibStep`](../../../waveflow/build/phase_calib.py) into per-direction access costs and whether it
serves one transaction at a time, and filed under the memory's identity. markov's build does it
([`markov_build.py`](../../../examples/markov/markov_build.py)):

```python
uncal = memory_harness(lambda sim: markov_memory(sim, calibrated=False), mem_script("calib"),
                       n_cycles=20000)
xsi = add_system_steps(dag, uncal, work_dir=work_dir, top="markov_mem_unit", prefix="umemcal_")
dag.add(MemCalibStep(sysm=uncal, work=xsi.work, prefix="umemcal_", store=store))
```

The memory then loads the record wherever it sits (its `calib_dir`), as a kernel's phases do. Next:
[Composing a parent](units_composing.md) from the records.
