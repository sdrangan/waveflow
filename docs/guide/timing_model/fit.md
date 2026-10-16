---
title: Calibrating a timed phase
parent: Timing Models
nav_order: 6
audience: python
snippets: run
api: [timed_ports, rtl_xfers, phase_corpus, fit_phase]
summary: "Calibrating a module's timed phases is generated from its declarations (waveflow/calib/phases.py): the ports to observe, their transfers read off an RTL trace, the pairing with pysim's phase records (pysim gives which transfers and the features, the RTL the times), and a per-span fit with interior points held out. The one hand-written piece is a scenario that produces the features. Shown re-fitting poly from its committed trace: response 4, latency 34, fill nwords - 1, held-out lengths exact. And the lesson poly taught: exact spans do not make an exact total when some of the module's time is in no span."
---

# Calibrating a timed phase

A declared phase predicts from its seed until it is calibrated.  Calibrating it means measuring each
span at RTL over a range of features and fitting the span's model -- and almost all of that is
generated from the declaration.

## What is generated, and what you write

From a module's declared phases, `waveflow/calib/phases.py` derives:

1. **The ports to observe** (`timed_ports`) -- every endpoint a phase names, under the attribute the
   module holds it in, which is the generated kernel's port of the same name.
2. **The RTL transfers** on them (`rtl_xfers`) -- read off a trace of those ports, framed by TLAST: per
   port, each transfer's index, first and last beat, word count, and whether the other side held it up.
   They are the same rows pysim logs.
3. **The pairing** (`phase_corpus`) -- **pysim supplies the pairing and the features; the RTL supplies
   the times.**  Each pysim phase record names its span's start and end as `(endpoint, transfer index,
   edge)`; the corpus looks both up in the RTL transfers.  This is legal because a phase's features are
   known before its first event, and it needs both sides to carry the same transfers on every port --
   **functional equivalence is a precondition of a timing fit** (the bit-exact check comes first).
4. **The fit** (`fit_phase`) -- each span on its clean rows, with the points you name held out, the
   parameters filed in the phase's `calib_dir`, where it loads them from.

What you write is the **scenario**: a workload that produces the features you need, at enough values.
Poly's is a run of nine commands, 8 to 200 samples (`examples/stream_inband/scenarios.py`, `calib`),
and a short driver, `examples/stream_inband/poly_calibrate.py`: run the scenario in pysim, run it traced
in cosim, and call the four steps.

## Re-fitting poly

The traced runs are committed, so the fit reproduces with no toolchain:

```python
from examples.stream_inband.poly_calibrate import HERE, calibrate

rep = calibrate(32, vcd=HERE / "vcd" / "calib_w32.vcd", file=False)
for name, ph in rep["phases"].items():
    for span, f in ph["fit"].items():
        print(name, span, {k: round(v, 2) for k, v in f["params"].items()})
        for feats, rtl, pred in f["holdout"]:
            print("    held out", feats, "rtl", rtl, "predicted", round(pred, 2))
```

```text
hdr resp_time {'intercept': 4.0}
samples latency {'nwords': 0.0, 'intercept': 34.0}
    held out {'nwords': 48} rtl 34.0 predicted 34.0
    held out {'nwords': 128} rtl 34.0 predicted 34.0
samples fill_time {'nwords': 1.0, 'intercept': -1.0}
    held out {'nwords': 48} rtl 47.0 predicted 47.0
    held out {'nwords': 128} rtl 127.0 predicted 127.0
```

A 4-cycle response, a 34-cycle pipeline, and a fill of one word a cycle (`ii·(m − 1)` with `ii = 1`,
[the loop form](./loops.md)).  The two held-out lengths -- interior points, never extrapolated -- are
predicted exactly.  At 64 bits the same run gives 4, 33 and `nwords − 1`.  The hand seeds poly used
before (`0`, `40`, `nwords`) were wrong on every span.

## Which measurements train a model

- **Only those the RTL did not hold up.**  A span is deterministic only while the environment does not
  stall the module inside it, so a row whose transfers the RTL shows stalled (an output not ready, an
  input not valid) tests a prediction but does not fit one.  pysim's own stall flags are reported
  beside it but do not decide: the target is the RTL's duration, and pysim's testbench is not the
  environment that was measured.
- **Only those whose load delivered what it asked for.**  An occurrence whose input burst ended early
  is marked `short`: its features describe the request, not what moved.
- **Over a real grid.**  At least three values of each feature, interior points held out, nothing
  predicted outside the range it was fitted on.

## Where the data comes from

The RTL side is a **trace of the kernel's ports**: any cycle-accurate run whose waveform records each
port's `TVALID` / `TREADY` / `TLAST`.  For poly it is a Vitis HLS co-simulation with port tracing
(`run.tcl`'s traced stage, in its own project so it never disturbs the timing scenario's report),
~5 minutes a width.  A unit harness under XSI (`plans/unit_harness.md`) is the other source, and the
same decode applies: the ports come from the declaration, never from someone naming a net.

## Exact spans, and a total that is not

The hand seeds poly used before had given its whole 32-bit transaction exactly cosim's 152 cycles --
right for the wrong reason: their errors cancelled (+6 on latency, −4 on the response, +1 on the fill).
With the spans fitted, pysim first came out at 148, and the gap was time **no declared span covered**:
at RTL the kernel starts reading the samples a cycle after the header while the response goes out four
cycles after it, and poly's body did the two in sequence.  Letting the body send the response while
the samples load -- as the RTL does -- made every stretch inside a command match the trace.

The total is now 145 against 152, and the 7 cycles are counted: one cycle per command of loop restart
that no span holds, and about six of start and done handshake (`ap_start` to the first header beat,
END to `ap_done`), which is the host's control path, not the module's work on its streams.  That is
the method working: an end-to-end number can be right by accident, spans make the accident visible,
and when the spans agree and the total does not, the difference is undeclared time -- to declare, or to
name and leave.

## As a build step

In the system flow, calibration is a framework step, not a script per example.  The **module** supplies
the one hand-written piece -- a workload whose scenario produces the feature values, and the values to
hold out -- and its harness (an `HwSystem` whose DUT is the module, generated from its boundary) turns
them into the calibrating system.  `add_calib_steps` adds that system to the build DAG as a second,
traced system (sharing `codegen` and `csynth`) followed by `PhaseCalibStep`, named `calib_phases`:

<!-- snippet: skip -->
```python
class PolyKernel(FreeRunMod):
    calib_holdout: ClassVar[dict] = {"nwords": (48, 128)}

    def workload(self, kind="test"):
        cmds = {"test": default_cmds, "calib": calib_cmds}[kind]()      # calib: nine lengths
        return {"s_in": stimulus(cmds)}

sysm = harness(PolyKernel, workload="test")
add_system_steps(dag, sysm, work_dir=WORK_DIR, top=TOP)
add_calib_steps(dag, sysm, work_dir=WORK_DIR, top=TOP,     # run with --through calib_phases
                store=ModuleStore(HERE / "calib"))
```

With a `store`, the fits are filed in the module record store, one timing record per calibrated module
under its identity -- the same key wherever the module sits, since what its ports are bound to is
context -- and a phase whose `calib_dir` is that store loads them from the record its module's identity
finds.  The store is asked first: when it already holds a record measured on this workload, the
calibration adds no simulation at all -- only a `PhaseCalibCachedStep`, which names the records it found.

`calib_phases` runs the workload in pysim (the records), decodes the traced XSI run at each kernel's
scope (the times), fits every span as above, files the parameters in each phase's `calib_dir`, and
writes the report -- every row's residual -- to `<work>/phase_calib.json`.  It runs only once the
workload's RTL recordings equal pysim's word for word (timing calibration presumes functional
equivalence), and a pysim occurrence with no RTL partner fails it rather than being fitted around.  The free-running poly
(`examples/stream_kernel`) is calibrated this way: latency 30, fill `nwords - 1`, response 4, held-out
lengths exact.

An on-chip memory is calibrated the same way, from its own harness rather than from declared phases.
`memory_harness` puts the memory behind a direct link and drives it with a scripted AXI-MM master that
records when each read and write starts and ends, in pysim and at RTL.  `MemCalibStep` (named
`<prefix>mem_timing`) fits what the memory adds per direction, `init + per_word * n` cycles, from the ops
that met no other, and whether a read and a write that met were served one after the other.  It files
the fit in the store under the memory's identity, and a `MemoryMod` given that store as its `calib_dir`
loads it.  markov's memory measures a read at `1 + n` cycles and a write at 1, serialized, and then
matches RTL to the cycle (`examples/markov/markov_build.py`, `umemcal_*`).

## Before timed phases

The framework's memory-mapped stream blocks were calibrated before timed phases existed, by fitting the
*residual* between an RTL run and pysim: [Component residuals](./component_residual.md), on top of the
platform's [bus-transfer model](./bus_model.md), with the shipped
[mem-stream residual](./memstream.md).  Those pages describe that method, which they still use until
they are declared as phases (memory-mapped events are on the plan).

## See also

- [What a timed phase is](./timed_phases.md) -- the figure is drawn from the trace fitted here.
- [The form of a span's model](./loops.md) -- reading `latency` and `ii` off the coefficients.
- [Model calibration](../calib/) -- `LinCalibModel`, the corpus, confidence: the layer underneath.
- [Timing Analysis Tools](../timing/) -- the cosim and trace tools the RTL side uses.
