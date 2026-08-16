---
title: Connecting the fabric side
parent: Python
grand_parent: RF converters
nav_order: 4
audience: python
api: [StreamIF, StreamIFMaster, StreamIFSlave, axis_bitwidth, samp_per_word, words_per_cycle]
summary: "Wiring the converter to your logic: an ordinary AXI-Stream at samp_per_word x nbits bits, the packing contract that decides which sample lands in which slot, why there is no spc but two derived rate ratios instead, the rate check the converter performs at pre_sim, and why a boundary port's depth declaration is silently discarded."
---

# Connecting the fabric side

This is the reassuring half. The converter's fabric side is an **ordinary** Waveflow AXI-Stream,
wired exactly as any other:

```python
adc_axis = StreamIF(name="adc_axis", sim=sim, clk=axis_clk, bitwidth=rfdc.axis_bitwidth)
adc_axis.bind("master", rfdc.rx_stream)
adc_axis.bind("slave", dut.s_in)
```

Your DSP block connects to a converter the same way it connects to anything else. There is no
parallel world to learn.

## The word width

```python
rfdc.axis_bitwidth      # samp_per_word * nbits
```

Read it off the converter rather than restating it. At `nbits=16, samp_per_word=4` that is a 64-bit
word carrying four sample slots, and 64 bits is the ceiling the constructor enforces.

`samp_per_word` is a `HwParam` and an **integer** because a sample cannot straddle a slot. It is the
one structural number at this boundary — and note it counts **slots**, not necessarily samples. Which
they are is what `iq_mode` decides.

## Real and I/Q: what `iq_mode` means {#iq-mode}

A wireless design wants **complex** samples. `iq_mode` says whether a beat's slots are real values or
interleaved I/Q pairs — and it does **not** change the width of the port:

| `iq_mode` | one beat carries | at `samp_per_word=4`, `nbits=16` |
|---|---|---|
| `0` | `samp_per_word` **real** samples | 4 real samples in a 64-bit word |
| `1` | `samp_per_word / 2` **complex** samples | 2 complex (I,Q) samples in a 64-bit word |

The port width is `samp_per_word * nbits` either way. That is deliberate: the width is a **hardware**
constraint — it is what the tile's AXI4-Stream actually is — so `iq_mode` reinterprets the slots
rather than resizing the bus. An I/Q pair occupies two adjacent slots, I in the lower.

**`iq_mode = 1` is not implemented yet.** The constructor refuses it rather than half-supporting it:

```
NotImplementedError: Rfdc stage 1 implements real samples only (iq_mode=0) ...
```

Two things are missing, and both are real work rather than a flag: the RF-side bundle format is
float64 and needs a manifest field to carry complex, and the quantizer's conformance twin covers real
`FixedField` only. Until those land, a complex design models I and Q as two real channels
(`n_ch = 2`), which is exactly what the hardware carries anyway — the difference is bookkeeping in the
model, not in the bits on the wire.

## The packing contract

Samples are packed **time-ascending from the LSBs**, each in a fixed `nbits` slot — the **oldest**
sample in the **least** significant slot. At `nbits=8`, the samples `[0, 64, -64, -128]` pack to
`0x80c04000`.

Do not hand-roll this. Packing goes through the
[generated array serializers](../../vectorization/), never a `.range()` you wrote, and the reason is
that `samp_per_word == 1` hides slot-order bugs entirely — order is unobservable with one sample per
word — so a bug you introduce at four samples per word will pass every test you thought to run at one.

## There is no `spc`

`samp_per_word` is the structural integer. Everything else at this boundary is a **ratio**, derived
and generally fractional:

| boundary | conversion | lives in |
|---|---|---|
| AXIS ↔ fabric | `samp_rate / (samp_per_word × f_axis)` words per cycle | the converter models (`RateTick`) |
| RF ↔ fabric | `samp_rate / (blksize × f_axis)` blocks per cycle | *implied* — see below |

**Derived, never declared.** Both terms already exist: `samp_rate` on the RF interface's clock,
`f_axis` on the AXIS interface's clock. The converter *reads* both rather than restating either, so
there is no third statement that could disagree. At 256 MSa/s into 300 MHz with four samples per word
that is `0.2133…` words per cycle — a number no integer expresses, which is why the model carries a
fractional-credit accumulator rather than a count.

The second conversion turns out **not to need its own object**. The block cadence follows from the
word rate: the ADC pulls a block when it has consumed the previous one's words, so the RF grid
emerges from `words_per_cycle` and the channel's depth bounds how far the source may run ahead. One
mechanism, one place.

## The rate check, and what it does not cover

At `pre_sim` — once the clocks are bound and it can see both rates — the converter refuses

```
samp_rate > samp_per_word * f_axis
```

with the arithmetic in the message. That is the **port's** capacity: one word per fabric cycle, times
the samples in a word.

**Your design is usually slower than its port**, and nothing in the converter knows that. A stage
that fires every two cycles absorbs half a sample per cycle at one sample per word, whatever the port
could have carried. Divide by the consuming task's `fire_cycles` and check the result yourself; that
is [rule 4](./rules.md#4-port-capacity-is-not-design-capacity), and skipping it cost a real design
1695 of 4096 samples.

## Do not declare a depth on these interfaces

A `StreamIF` that becomes a **top-level port** cannot carry a FIFO depth. Vitis ignores the pragma
(`HLS 214-387`) — in one placement, silently — and the RTL gets the default of 2 whatever the Python
says. `composite_top_spec` now refuses the declaration outright, because a depth that is silently 2
is worse than no depth: the number in the Python reads like a fact.

An **internal** channel's depth *is* emitted and *is* physical. That asymmetry is not a footnote; it
is why an elastic buffer in front of a converter has to be a task plus an internal channel rather
than a bigger number on the port. See [rule 7](./rules.md#7-internal-depth-is-physical-a-boundary-ports-is-not).

## Next

- [Block sampling](./sampling.md) — the model underneath both sides.
- [The design rules](./rules.md) — including the two this page hands off to.

**Source of truth:** `examples/rf_loopback/rfdc.py`, `waveflow/build/xsi/xsi_rfdc.h`,
`tests/build/test_xsi_rfdc_samp.py`.
