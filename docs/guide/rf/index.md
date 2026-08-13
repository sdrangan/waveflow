---
title: RF converters
parent: Guide
nav_order: 10.25
has_children: true
audience: python
summary: "Modelling a design that talks to an RF data converter — an ADC/DAC such as the RFDC on an AMD RFSoC. A converter is not just another AXI-Stream peer: it has its own clock, it cannot be back-pressured on the sample side, and its samples are worth simulating in blocks rather than one at a time. This section covers the block-level sampling model those three facts produce, and the three-block decomposition — digital logic, the converter, and the RF environment — that keeps them apart."
---

# RF converters

Many applications, wireless ones especially, connect to an RF data converter — an ADC/DAC block such
as the RFDC on an AMD/Xilinx RFSoC part. This section is about modelling a design that has one.

## A converter is not just another AXI-Stream peer

From the fabric it looks like one: words arrive on a stream, words leave on a stream. Three things
make it different, and each of them shapes the model.

**It has its own clock.** The sample rate is not the fabric clock and is not a multiple of it. A
converter running at 256 MSa/s into a 300 MHz fabric produces `256/(4·300) ≈ 0.213` words per AXI
cycle at four samples per word — a *ratio*, not a count. Whatever else changes, that number is
fractional and derived.

**Half of it cannot be back-pressured.** On the fabric side, `TREADY` works: the converter has a
real input FIFO and it does stall the logic feeding it. On the *sample* side there is no such signal
in either direction. An ADC presents samples whether or not anyone is ready, and a DAC emits
whatever is in its FIFO when a sample period comes due — including nothing. Backpressure protects
against over-production and there is no mechanism at all that protects against under-production, so
loss has to be **counted** rather than prevented.

**Simulating one sample at a time is the wrong granularity.** A wireless simulation worth running
covers milliseconds of signal; at hundreds of megasamples per second that is hundreds of millions of
events. The whole model here rests on moving a *block* of samples per event and doing the arithmetic
in NumPy — which is also, conveniently, the granularity at which most feed-forward DSP is exact.

## Three kinds of block

A design with a converter in it has three kinds of thing in it, and the boundaries between them are
load-bearing:

| | what it is | where it ends up |
|---|---|---|
| **digital logic** | synthesizable hardware processing the converter's samples — filters, FFTs, buffers | an `hls::task` inside the generated top |
| **the converter** | a model of the ADC/DAC presenting the same interfaces as the real IP | beside the top (later: the real IP) |
| **RF environment** | channel, sources, sinks | simulation only; never synthesized |

All three are plain [`HwModule`s](../flows/modules.md). There is no separate class for "participates
in simulation but is never synthesized" — which side of the boundary a module falls on is a property
of the **[cut](../flows/modules.md#the-cut)**, chosen per build, and freezing a per-build role into a
class fact is exactly the mistake that would make the converter unusable in the third case (a real
bitstream, where the model is replaced by AMD's IP and *the digital logic must not have to change its
interface*).

What differs between them is only which **realization hooks** each declares — and an RF source or
sink declares neither, which is a *finding* from `check`, not something it says about itself:

```pycon
>>> check(RfDataSource, "xsi_bfm_model")
(False, 'RfDataSource declares no bfm_model() hook, so it has no pre-written cycle model ...')
```

## Pages

- [Block sampling](./sampling.md) — the sampling model: the block as the transaction, `blksize` as
  the fidelity knob, the absolute-grid metronome, `t0` and the sample grid, and the loss counters.
- [The converter](./converter.md) — the `Rfdc` module and its two RTL-side models: the AXI-Stream
  packing contract, `samp_per_word` versus the two derived rate conversions, bit-exact quantization,
  and the underflow/overflow contract.

One further page is planned and deliberately **not written yet**: the fidelity boundary — what block
granularity can and cannot resolve. It needs a real DSP block and a channel to demonstrate both
halves, and neither exists; a page written from a plan rather than from working code is the specific
failure this schedule exists to prevent. See `plans/adc_model.md`.

## See also

- [RF loopback](../../examples/rf_loopback/) — the worked example this section is written from.
- [Hardware modules](../flows/modules.md) — the kinds, the hooks, and the cut.
- [Interfaces](../interface/) — the transactional model `RFSampIF` is an instance of.
