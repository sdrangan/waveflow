---
title: The polynomial's timing
parent: Free-running polynomial
nav_order: 5
---

# Timing

pysim's timing for the kernel comes from two **declared timed phases** -- spans between events at its
own ports, with the features their durations depend on
([Timed phases](../../guide/timing_model/timed_phases.md)):

```python
self.samp_phase = TimedPhase(
    "samples", spans={"latency": (self.s_in.first, self.m_out.first),
                      "fill_time": (self.m_out.first, self.m_out.last)},
    features=("nwords",), calib_dir=HERE / "calib")
self.resp_phase = TimedPhase(
    "resp", spans={"resp_time": (self.m_out.last, self.m_resp.first)}, calib_dir=HERE / "calib")
```

- **samples** is pipelined: `latency` from the first sample word in to the first result word out, then
  `fill_time` over the results -- the loop that reads a word, computes, writes a word.
- **resp** is sequential: `resp_time` from the last result word to the response's first word.

Every event is a handshake at a port, so every span can be measured at RTL.

## Calibrating them in the harness

The kernel names its calibration scenario and what to hold out of the fit:

```python
calib_holdout: ClassVar[dict] = {"nwords": CALIB_HOLDOUT}     # (48, 128): interior lengths test the fit

def workload(self, kind="test"):
    cmds = {"test": default_cmds, "calib": calib_cmds}[kind]()  # calib: nine lengths, 8 to 200
    return {"s_in": stimulus(cmds)}
```

`add_calib_steps` adds the calibration as a second, traced system sharing `codegen` and `csynth`, and
`calib_phases` fits it ([Fitting the model](../../guide/timing_model/fit.md)): pysim pairs the events
and supplies the features, the RTL trace supplies the times. Measured (Vitis 2025.1):

| span | fit | residual |
|---|---|---|
| `latency` | 30 cycles | 0 |
| `fill_time` | `nwords - 1` cycles | 0 |
| `resp_time` | 4 cycles | 0 |

The held-out lengths are predicted exactly too.

## Filed under the kernel's identity

The fit is one record in [`calib/`](../../../examples/stream_kernel/calib), filed under the kernel's
**identity** -- a digest of its structure, free of what its ports are bound to -- with the phases it was
fitted to. Wherever the kernel sits (its harness here, a memory-mapped device on the next page), its
phases load the record its identity finds. A repeat of the calibration finds the record and runs no
simulation; a kernel whose phases changed finds none, and is calibrated again.

Next, [Behind AXI-MM](memory_mapped.md): the same kernel, its timing composed from this record.
