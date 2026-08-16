---
title: Block sampling
parent: Python
grand_parent: RF converters
nav_order: 5
audience: python
api: [RFSampIF, RFSampIFTx, RFSampIFRx, RfBlock, set_t0, samp_time, assert_clean, counters]
summary: "The model underneath the wiring pages: one SimPy event carries one (n_ch, blksize) block of samples, the block duration is the timing, and NumPy is the function. Covers blksize as the fidelity/speed knob; why the metronome lives in the interface rather than in a node; why it schedules on an absolute grid and a relative timeout loop demonstrably slips; t0 and the sample grid; and the underrun/overrun asymmetry the counters exist to record."
---

# Block sampling

The RF sample channel is an **[interface](../../interface/)**, not a module:
[`RFSampIF`](../../../../waveflow/hw/rf_sample_if.py). It owns the sample-rate clock, the block cadence,
a buffer, and two loss counters — and because an `Interface` is already a
[`SimObj`](../../sim/), it already has a `run_proc` to run them in.

```python
adc_if = RFSampIF(name="adc_if", sim=sim, samp_clk=Clock(freq=256e6),
                  n_ch=1, blksize=256, n_blk=8)
adc_if.bind("tx", source.rf_ep)      # the producer
adc_if.bind("rx", rfdc.rx_rf)        # the consumer
```

## The block is the transaction

One SimPy event carries one **`(n_ch, blksize)` block** — every channel of a tile, one block period
of samples, as a single NumPy array:

- the **block** is the transaction,
- **NumPy** is the function,
- the **block duration**, `blksize / samp_rate`, is the timing.

That is the whole model, and every other decision on this page follows from wanting it to stay that
simple.

### `blksize` is the fidelity/speed knob

It is the one number that trades resolution against speed, and it does so linearly: halving it
doubles the event count and halves the time granularity at which anything can be observed. It is *not*
a correctness knob for feed-forward processing — a filter applied block-wise gives the same answer at
any `blksize`, provided the block spans the state it needs.

### All channels ride one interface

A four-channel tile is one `RFSampIF` carrying `(4, blksize)`, not four interfaces carrying
`(1, blksize)`. Splitting per channel would produce `n_ch` events per block period and work against
the entire reason for block granularity. The channels of a tile share one grid and one scalar
[`t0`](#t0-is-the-synchronization-primitive).

Independent grids and per-channel skew are both handled elsewhere, and
[connecting the RF side](./rf_side.md#one-interface-per-direction) says how — along with why the
interface is unidirectional.

## The metronome lives in the edge

Nothing pulls and nothing polls: the interface's own `run_proc` emits one block every block period.

This is the idiomatic place for it rather than a convenient one. `StreamIF.depth` already
establishes the precedent — an edge owning a *physical* property, single-sourced and read by both
backends — and the XSI testbench walk is edge-owned by design. Putting the clock on the wire also
means neither node has to know the other's rate.

Backpressure then falls out in the right direction. `RFSampIFTx.put()` yields while the buffer is
full, so a producer runs **at most `depth` blocks ahead** of the metronome: the RF environment
computes with bounded lookahead rather than free-running into memory.

### Schedule on an absolute grid {#absolute-grid}

Block *k* fires at `t0 + k · blk_period`, recomputed from the epoch every time:

```python
k = 0
while True:
    k += 1
    yield self.timeout(self.t0 + k * self.blk_period - self.env.now)
    yield from self._drain_one(k)          # the body may now yield freely
```

**Not** `yield self.timeout(blk_period)` in a loop. That form restarts each period from wherever
`env.now` happens to be when the body finishes, so anything the body yields for — a blocking push, an
edge that charges transfer time, a `timeout(0)` in a callback — is added to the grid and never given
back.

This is worth stating carefully because it is the claim most likely to be waved away, so it is
**demonstrated rather than asserted**. `tests/hw/test_rf_sample_if.py::TestMetronome` builds the
rejected scheduler and runs it:

| | block 1 | block 2 | … | block 6 | error at block 6 |
|---|---|---|---|---|---|
| `timeout(period)` in a loop | 1.0 s | 2.1 s | … | 6.5 s | **0.5 s — half a block period** |
| absolute grid | 1.0 s | 2.0 s | … | 6.0 s | 0 s |

Both rows are 6 blocks of a 1.0 s period with a body that yields for 0.1 s; the second row is the
real `RFSampIF` driven through the same yielding body via its `_drain_one` seam. The error in the
first row is `(k-1)·0.1 s` — **proportional to the block index**, not a constant offset. That is what
makes it fatal for a sample clock rather than merely untidy: a fixed lag is an epoch you can measure
and correct, while a slipping one is a sample rate that is quietly wrong.

Today's obvious case is already avoided by the non-blocking receiver (below); absolute scheduling
makes the property **structural** instead of one refactor away.

The related guard is that the metronome may not silently fall behind at all: a body that outlasts a
block period raises rather than slipping.

## `t0` is the synchronization primitive {#t0-is-the-synchronization-primitive}

Sample *n* occurs at `t0 + n / samp_rate`, on every channel. Two numbers define the entire grid, so
alignment across TX/RX and across antennas is **derived and assertable** rather than emergent from
scheduling coincidence:

```python
lag = tb.dac_if.samp_time(n) - tb.adc_if.samp_time(n)   # same for every n
```

`t0` is **owned by the converter and pushed onto the interface at bind**, while the sample rate
travels the other way — [connecting the RF side](./rf_side.md#t0-and-why-alignment-is-derived) has
the mechanics and the two reasons the epoch-plus-rate formulation is worth having (unequal tile rates,
and MTS).

What matters for the *model* is the consequence: a loop through the RF grids — ADC into the fabric
and back out of the DAC — costs at least one block *index*, because the ADC only delivers block *k*
at the instant the DAC period for it comes due. That is structural. A zero-latency fabric would not
close it either, and it is the "no dependency within < `blksize` samples" limit applied to the fabric
path.

The cost belongs to the pipeline, which declares it (`blk_latency`, ≥ 1 for any block-processing
module) and gets checked against the DAC edge's startup underruns. A loop that declares zero block
latency is refused at elaboration rather than reported later as an underrun — it is not a slow
system, it is not a system.

## The counters are the contract

There is a real asymmetry in the hardware, and the interface captures both halves:

| | signalled? | what the model does |
|---|---|---|
| buffer **full** → over-production | yes — a real input FIFO stalls the fabric | `put()` **yields** |
| buffer **empty** → under-production | **no** — nothing in AXI-Stream can express "you were late" | **zero-fill** and count |
| receiver full → over-delivery | no — the converter presents samples regardless | **drop** and count |

Delivery is deliberately **non-blocking**: the receiver takes the block or it is dropped. A blocking
receiver would push the grid, and a design that silently slips its sample clock is precisely what the
absolute schedule exists to make impossible.

Zero-fill is the right filler — deterministic, visible in the RF output, and it does not hide the
error. But **the padding is not the contract; the counters are**, and an `RfBlock` carries its grid
index alongside its samples, so a drop leaves a visible gap rather than shifting everything after it.

[Reading them, and asserting them](./rf_side.md#reading-the-counters) is the how-to;
[rule 5](./rules.md#5-the-counters-are-the-contract) is why it is not optional.

> The counters exist in the Python model today. The obligation that the *generated RTL model* produce
> the same numbers for the same scenario is a separate gate and is not yet built; see
> `plans/behavioral_edges.md`.

## Signal processing stays out of the interface

An edge may own **transport** — rate, buffering, ordering, loss accounting. It must not own **signal
processing**: gain, fractional delay, and multipath belong in a channel block. Three reasons, and the
third is the one that decides it:

1. **The equivalence obligation.** Every behaviour in an interface must be reproduced by hand in its
   C++ model, and nothing checks that they agree, so the bar is "obviously the same in ten lines".
   Zero-fill plus two counters clears it; a multipath channel with fractional delays and Doppler is a
   DSP library you would then have to prove bit-exact against NumPy.
2. **Inter-block state.** A channel has memory spanning block boundaries (overlap-save, a Doppler
   phase accumulator). `RFSampIF` is stateless with respect to signal *content* — it moves whole
   blocks and accounts for loss.
3. **Asymmetric cost.** Adding a channel block later is purely additive. Removing behaviour from an
   interface later means rewriting its C++ model and re-verifying the gate. "Add it later" is true in
   one direction only.

Two of the three temptations are already covered elsewhere. **Bulk delay is `t0`** — sample *n*
arrives at `t0 + n/fs`, so raising `t0` delays everything, and only *fractional* and *per-path*
delays are filters. **Gain is not an interface property**: it interacts with quantization, which is
the converter's job, so it splits the way the hardware does — an amplitude reference on the
converter, path loss in the channel. Accept a scalar gain on the edge and the next request is a
frequency-dependent one, which is a filter in the transport layer by accident.

## Next

- [The capture buffer](./capture.md) — the first RF block that does something, and the page that
  needs the sample grid.
- [RF loopback](../../../examples/rf_loopback/) — the worked example: a converter, a pass-through, and
  the loss gate.

**Source of truth:** `waveflow/hw/rf_sample_if.py`; the metronome demonstration is
`tests/hw/test_rf_sample_if.py::TestMetronome`.
