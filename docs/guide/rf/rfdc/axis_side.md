---
title: Connecting the fabric side
parent: Rfdc
grand_parent: RF converters
nav_order: 7
audience: python
api: [StreamIF, StreamIFMaster, StreamIFSlave, axis_bitwidth, RfdcSampWord, words_per_cycle]
summary: "Wiring the converter to your logic: an ordinary AXI-Stream at the word type's width, why there is no samples-per-cycle parameter but two derived and generally fractional ratios instead, the rate check the converter performs at pre_sim and the larger one it cannot perform for you, and why a depth declared on a boundary port is silently discarded."
---

# Connecting the fabric side

This is the reassuring half. The converter's fabric side is an **ordinary** Waveflow AXI-Stream,
wired exactly as any other:

```python
adc_axis = StreamIF(name="adc_axis", sim=sim, clk=axis_clk, bitwidth=rfdc.axis_bitwidth)
adc_axis.bind("master", rfdc.rx_streams[0])
adc_axis.bind("slave", dut.s_in)
```

Your DSP block connects to a converter the same way it connects to anything else. There is no
parallel world to learn.

**One of these per channel.** `rfdc.rx_streams` / `rfdc.tx_streams` hold one endpoint per RF
channel, and row `ch` of a block is what port `ch` carries — so an `n_ch`-channel converter is
`n_ch` ordinary streams, not one wide one. Everything on this page is per port.

## The width comes from the word type

```python
rfdc.axis_bitwidth        # read it; never restate it
```

It is `samp_per_word × bits_per_samp_pack` (× 2 for I/Q), and it belongs to the
[sample word](./word.md) — which is also where the packing convention, the effective-vs-container
distinction and `justify` live. This page is about everything *else* at the boundary.

## There is no samples-per-cycle parameter

`samp_per_word` is the one structural integer here. Everything else is a **ratio** — derived, and
generally fractional:

| boundary | conversion | lives in |
|---|---|---|
| AXIS ↔ fabric | `samp_rate / (word.samp_per_word × f_axis)` words per cycle | the converter models (`RateTick`) |
| RF ↔ fabric | `samp_rate / (blksize × f_axis)` blocks per cycle | *implied* — see below |

**Derived, never declared.** Both terms already exist: `samp_rate` on the RF interface's clock,
`f_axis` on the AXIS interface's clock. The converter *reads* both rather than restating either, so
there is no third statement that could disagree with them.

At 256 MSa/s into 250 MHz with four samples per word that is `0.256` words per cycle — a number no
integer expresses, which is why the model carries a fractional-credit accumulator rather than a
count. **That is the reason there is no `spc` parameter**: any integer you wrote down would be wrong.

### Which 250 MHz, and why 250

250 MHz is `RFSOC4X2_CLK_HZ`, what the RF examples build for, and it is **forced by the geometry
rather than chosen**: an AXIS word carries a whole number of samples, so
`f_axis = samp_rate / samp_per_word`. A 1 GSa/s converter gives exactly four samples per word at
250 MHz, where 300 MHz would need `3.33`, which is not a sample count.

A premise is only worth writing against if it is reachable, so — the RF examples synthesize for the
**RFSoC 4x2** (`xczu48dr-ffvg1517-2-e`) and close 250 MHz with margin:

| example | Fmax |
|---|---|
| capture buffer (`rf_samp_buf_rx`) | 365 MHz |
| playout buffer (`rf_samp_buf_tx`) | 432 MHz |
| loopback DUT (`rf_pass_through`) | 371 MHz |

Those are **recomputed from each solution's own `csynth.xml`** by `tests/docs`, not transcribed — a
figure that drifts from the report fails a test rather than quietly misleading you.

The second conversion needs no object of its own. The block cadence follows from the word rate — the
ADC pulls a block once it has consumed the previous one's words — so the RF grid emerges from
`words_per_cycle`, and the channel's depth bounds how far the source may run ahead. One mechanism,
one place.

## The check the converter performs

At `pre_sim`, once both clocks are bound and it can see both rates, the converter refuses

```
samp_rate > word.samp_per_word × f_axis
```

with the arithmetic in the message. That is the **port's** capacity: one word per fabric cycle, times
the samples in a word.

## The check it cannot perform for you

**Your design is almost always slower than its port, and nothing in the converter knows that.**

A stage that fires every two cycles absorbs half a sample per cycle at one sample per word, whatever
the port could have carried. The number that matters is:

```
design capacity  =  samp_per_word × f_axis / cycles_per_word
```

Divide by the consuming stage's cost and check the result yourself. This is
[rule 4](./rules.md#4-port-capacity-is-not-design-capacity), and skipping it cost a real design
**1695 of 4096 samples** — a run that produced well-formed output and lost 41% of it.

The converter cannot do this check because it cannot see inside your logic. It knows what the port
can carry; only you know what you will do with it.

## Overflow and underflow are counted, not prevented

`TREADY` works on the fabric side: the converter has a real input FIFO and it does stall the logic
feeding it. On the **sample** side there is no such signal in either direction. An ADC presents its
samples whether or not anyone is ready, and a DAC emits whatever is in its FIFO when a sample period
comes due — including nothing. Back-pressure protects against over-production and nothing at all
protects against under-production, so loss is **counted rather than prevented**:

| | what happened | where it shows |
|---|---|---|
| **overflow** | the fabric did not take a word the ADC presented | `StreamIF.dropped`, and `last_drop_time` |
| **underflow** | a block period came due with nothing to play; the block is zero-filled | `RFSampIF.underrun` |
| **overrun** | the receiver would not take a block that was ready | `RFSampIF.overrun` |

The zero-fill is deterministic and visible in the output, but **the padding is not the contract — the
counters are.** `RFSampIF.counters()` returns all four numbers, and asserting
`underrun == 0 and overrun == 0` is what turns a design that would fail on hardware into one that
fails in simulation.

**You usually do not handle this yourself.** [the `RfShotBuf` and `RfStreamBuf` families](../choosing.md) sit
between the converter and your logic precisely so the never-miss-a-deadline obligation is theirs, and
it is discharged once there rather than in every design. The counters are what you reach for when you
consume the converter's stream **directly** — which is the right answer for a filter or a detector,
and the case [rule 4](./rules.md#4-port-capacity-is-not-design-capacity) exists for.

## Do not declare a depth on these interfaces

A `StreamIF` that becomes a **top-level port** cannot carry a FIFO depth. Vitis ignores the pragma
(`HLS 214-387`) — in one placement, silently — and the RTL gets the default of 2 whatever the Python
says. `composite_top_spec` refuses the declaration outright, because a depth that is silently 2 is
worse than no depth: the number in the Python *reads like a fact*.

An **internal** channel's depth **is** emitted and **is** physical. That asymmetry is not a footnote —
it is why an elastic buffer in front of a converter has to be a task plus an internal channel rather
than a bigger number on the port. See
[rule 7](./rules.md#7-internal-depth-is-physical-a-boundary-ports-is-not).

## Next

- [The sample word](./word.md) — why converters pack, and the convention that says which sample
  lands where.
- [Block sampling](./sampling.md) — the model underneath both sides.
- [The design rules](./rules.md) — including the two this page hands off to.

**Source of truth:** `examples/rf_loopback/rfdc.py`, `waveflow/build/xsi/xsi_rfdc.h`,
`tests/build/test_xsi_rfdc_samp.py`.
