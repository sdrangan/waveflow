---
title: RF converters
parent: Guide
nav_order: 10.25
has_children: true
audience: python
summary: "Modelling a design that talks to an RF data converter — an ADC/DAC such as the RFDC on an AMD RFSoC. A converter is not just another AXI-Stream peer: it has its own clock, half of it cannot be back-pressured, and its samples are worth simulating in blocks rather than one at a time. This page says what those three facts cost you and maps the rest of the section."
---

# RF converters

Many applications, wireless ones especially, connect to an RF data converter — an ADC/DAC block such
as the RFDC on an AMD/Xilinx RFSoC part. This section is about modelling a design that has one.

## What `Rfdc` is, and what it is not

`Rfdc` is a Waveflow `HwModule` modelling a **simplified emulation** of the
[AMD RF Data Converter LogiCORE IP](https://docs.amd.com/r/en-US/pg269-rf-data-converter/Introduction).
It is designed to model **the interface the logic would see**, not the converter's internals.

Like the AMD block, it exposes **two AXI4-Stream interfaces**, one per direction, with time origins
that can hold a fixed relation — the modelling counterpart of multi-tile synchronisation. Its RF side
simulates the signals that would be emitted by, or presented to, the physical converter, and
optionally the RF up/down conversion attached to the AMD IP.

What that framing buys you is the thing worth holding on to: **your logic sees the same boundary in
simulation that it will see in hardware.** What it does not buy you is the converter's own behaviour —
tile calibration, PLL and clocking, MTS bring-up, the digital mixers' exact arithmetic. Those are
either configuration you supply as a measured constant or, where they cannot be modelled at all, named
as out of scope on the page that would otherwise imply them.

## A converter is not just another AXI-Stream peer

From the fabric it looks like one: words arrive on a stream, words leave on a stream. Three things
make it different, and every page here follows from one of them.

**It has its own clock.** The sample rate is not the fabric clock and is not a multiple of it. A
converter running at 256 MSa/s into a 250 MHz fabric produces `256/(4·250) ≈ 0.256` words per AXI
cycle at four samples per word — a *ratio*, not a count. Whatever else changes, that number is
fractional and derived.

> **Which 250 MHz fabric, and why 250.** Every rate on these pages assumes one, so it is worth
> saying where it comes from. The RF examples synthesize for the **RFSoC 4x2**
> (`xczu48dr-ffvg1517-2-e`) at 250 MHz, and close it with margin — 365 MHz on the capture buffer,
> 432 on the playout buffer, 371 on the loopback DUT.
>
> **These held when the converter-facing bodies were pipelined to one word per cycle on 2026-08-18.**
> That was worth checking rather than assuming — an II=1 loop does in one cycle what used to take
> three, so its critical path is longer — and an intermediate version of that work did cost the
> playout buffer 150 MHz. It was reverted for an unrelated correctness reason, and the figures above
> are the shipped design's.
>
> **250 is forced by the geometry rather than chosen.** An AXIS word carries an integer number of
> samples — a sample cannot straddle a slot — so `f_axis = samp_rate / samp_per_word`. For a 1 GSPS
> converter that gives `1000/250 = 4` samples per word exactly, where 300 MHz would need `3.33`,
> which is not a sample count. These pages said 300 MHz until 2026-08-18; that came from the
> illustrative arithmetic above and propagated, and it was never an engineering choice.
>
> The part is chosen *per example*: the non-RF examples stay on the Zynq-7020 their cycle gates and
> the calibration corpus were measured against. Until 2026-08-17 the RF examples were built for that
> Zynq too, which reached 137 MHz — so the premise these pages are written against was not reachable
> on the part that was actually being synthesized.

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

What differs between them is only which **realization hooks** each declares, and that is a *finding*
from `check` rather than something a module says about itself. The RF loopback example
[shows both answers side by side](../../examples/rf_loopback/rtl.md#what-check-says-about-these-modules)
— run there rather than quoted here, so it cannot go stale in two places at once.

## The map

Two arcs, and they are in the order you need them.

**[Python](./python/)** — do it, then understand it, then learn what it cannot tell you. Start at the
[quickstart](./python/quickstart.md); it is the shortest path to samples flowing. The
[design rules](./python/rules.md) are the page to read before writing code of your own — seven things
that make a design wrong if you break them.

**XSI** — what changes at RTL. Not written yet; the Python arc's
[fidelity boundary](./python/fidelity.md) ends by saying exactly what needs it, which is the honest
argument for that section existing.

## See also

- [RF loopback](../../examples/rf_loopback/) — the worked example this section is written from.
- [Hardware modules](../flows/modules.md) — the kinds, the hooks, and the cut.
- [Interfaces](../interface/) — the transactional model `RFSampIF` is an instance of.
