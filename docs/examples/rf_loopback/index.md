---
title: RF loopback
parent: Examples
nav_order: 10
has_children: true
summary: "The worked example for designs that talk to an RF data converter. A source plays sample blocks into an ADC, the samples cross into the fabric as AXI-Stream words, a trivial pass-through relays them, and a DAC turns them back into sample blocks at a sink — a loopback that is byte-identical end to end, one block later. Deliberately without DSP: the point is the converter boundary itself, and the loss counters that are the only evidence a sample grid was actually met."
---

# RF loopback — a design with a data converter

This is the worked example for the [RF converter guide](../../guide/rf/). It is the smallest graph
that has a converter in it:

```
RfDataSource --RFSampIF--> Rfdc.rx_rf | Rfdc.rx_stream --StreamIF--> RfSampPassThrough
                                                                              |
RfDataSink   <--RFSampIF-- Rfdc.tx_rf | Rfdc.tx_stream <--StreamIF------------+
```

Five nodes, four edges, and no signal processing anywhere. That is on purpose. Every other example
in this collection is about what a kernel *computes*; this one is about the **boundary** the samples
cross to reach a kernel at all — a boundary with its own clock, a granularity mismatch, and a failure
mode that no protocol signal reports.

## Learning objectives

- Model an RF sample channel as an [**interface** that owns a metronome](../../guide/rf/sampling.md)
  — a clock, a block cadence, a buffer, and loss counters living on the edge rather than in a node.
- Model a **data converter** as a module carrying both directions, with `HwParam` structure
  (resolution, samples per word) separated from `DynParam` knobs (the amplitude reference).
- Quantize bit-exactly with the integer-backed `FixedField` and pack samples into stream words
  through the generated array serializers.
- Assert a **byte-identical** loopback (shifted by the pipeline's declared block latency) *and* that loss is exactly what the graph declared, and understand why the
  first check is not sufficient without the second.
- Read `check(mod, "xsi_bfm_model")` as a **finding** about a module rather than a declaration on it.

## Where the pieces live

| | file | role |
|---|---|---|
| the edge | `waveflow/hw/rf_sample_if.py` | `RFSampIF` — framework, generic to any converter |
| the RF environment | `waveflow/simulation/rf_tb.py` | `RfDataSource` / `RfDataSink` — framework, bundle-backed |
| the converter | `examples/rf_loopback/rfdc.py` | `Rfdc` |
| logic + graph | `examples/rf_loopback/rf_loopback.py` | `RfSampPassThrough`, `RfLoopbackTB`, `RfLoopbackSim` |

## Pages

- [Python model](./python.md) — the graph, the converter, the loopback gate, and the two faults that
  make the loss counters mean something.

The digital logic is [synthesized and proved at RTL](./python.md#synthesis) — cut **alone**, between
generic AXI-Stream BFMs. A page for the *converter* at RTL is not written: the models exist but
nothing wires them into a graph yet, so its rate conversions and counter-equivalence gate would be
written from the plan rather than from working code. See `plans/adc_model.md`.

## See also

- [RF converters](../../guide/rf/) — the concepts this example is written from.
- [Free-running memory copy](../memcpy/) — the graph/procedure split and the bundle discipline this
  example reuses.
