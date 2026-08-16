---
title: Connecting the RF side
parent: Python
grand_parent: RF converters
nav_order: 3
audience: python
api: [RFSampIF, RFSampIFTx, RFSampIFRx, RfDataSource, RfDataSink, set_t0, samp_time, counters, assert_clean]
summary: "Wiring the converter's sample side: one RFSampIF per direction, what each of its parameters means, the file-backed source and sink that stand in for the RF environment, and t0 — pushed by the converter, read back as the thing that makes alignment a derived assertion rather than a scheduling coincidence."
---

# Connecting the RF side

The RF side is the one part of an RF design that is genuinely new. It is **one interface**, plus a
producer and a consumer to sit on either end of it.

```python
adc_if = RFSampIF(name="adc_if", sim=sim, samp_clk=Clock(freq=256e6),
                  n_ch=1, blksize=256, n_blk=8)
adc_if.bind("tx", source.rf_ep)      # the producer
adc_if.bind("rx", rfdc.rx_rf)        # the consumer
```

`RFSampIF` is an [interface](../../interface/), not a module, and it owns rather more than a wire
usually does: the sample-rate clock, the block cadence, a buffer, and two loss counters. Because an
`Interface` is already a [`SimObj`](../../sim/), it has a `run_proc` to run them in.

It **exists only in simulation**. The converter's RF side is analogue pins, so there is no RTL
counterpart to write — which is why nothing here has a depth pragma or a handshake.

## Its parameters

| parameter | what it means |
|---|---|
| `samp_clk` | the **sample-rate** clock. This is where the sample rate is *declared*; a converter reads it at bind rather than carrying a copy |
| `n_ch` | channels carried per block — all of a tile's channels ride one interface |
| `blksize` | samples per channel per block: the [fidelity/speed knob](./sampling.md#blksize-is-the-fidelityspeed-knob) |
| `depth` | producer-side buffer, in **blocks**. `put()` yields when it is full, so a producer runs at most `depth` blocks ahead |
| `n_blk` | how many block periods the metronome runs, or `None` for unbounded (which then needs an `env.run(until=…)` bound) |

`n_blk` is what makes a run terminate on its own. Setting it *larger* than the number of blocks your
source supplies is how you provoke an underrun on purpose — a testbench knob, not a design one.

## One interface per direction

`RFSampIF` is **unidirectional**, and a converter therefore takes two.

TX and RX share exactly one quantity — the time origin — and differ in every other: sample rate,
channel count (four ADC and two DAC on an RFSoC 4x2), `blksize`, buffer, counters, and peer. A
bidirectional interface would carry `(fs_tx, fs_rx)`, `(n_tx, n_rx)`, two buffers and two metronomes,
and every consumer would pay for the duality. The counters make the same point: **underrun is a TX
concept and overrun an RX concept**, so kept apart each object has exactly one natural failure mode.

A genuinely symmetric case — a TDD antenna port — is a *pair* of interfaces held by one node, which
costs nothing.

Channels that need **independent grids** are not one tile; give them their own interface. Per-channel
*skew* is a different thing again and does not live here: `t0` is an epoch (when a counter starts, a
tile property) while skew is a delay (how much later a path delivers, a path property). Every channel
rides one block delivered by one event, so no per-channel offset could change when samples arrive —
applying one would mean shifting samples inside a block, which is signal processing, which an edge
does not do. Measured skew belongs where it can be acted on: a channel or DSP block.

## The environment: a source and a sink

`RfDataSource` and `RfDataSink` are the RF environment for a testbench. Both are **file-backed**, and
that is the discipline rather than a convenience — one on-disk bundle drives the Python run *and* the
later RTL run, so the two backends can never start from different bytes.

```python
source = RfDataSource(name="src", sim=sim, in_bundle="vectors/rf_in", start_delay=0.0)
sink = RfDataSink(name="sink", sim=sim, out_bundle="vectors/rf_out", depth=2)
```

| field | on | what it does |
|---|---|---|
| `in_bundle` / `out_bundle` | both | the bundle path — a `DynParam`, so a stable relative string, never a temp path |
| `root` | both | run-time anchor for that path, set by whoever materializes the scenario |
| `start_delay` | source | seconds before the first block. **Fault injection**: the grid does not wait, so every period that elapses first is a counted underrun |
| `depth` | sink | receiver queue depth in blocks |
| `stall_after` | sink | **fault injection**: stop consuming after this many blocks, so the queue fills and overruns start |

The source loads its bundle in `pre_sim` and the sink dumps its capture in `post_sim`, which makes a
loopback a **file-to-file byte comparison** rather than an array comparison in memory.

The two fault knobs are there because a counter that has never counted is not evidence that it works.
Use them; see [rule 5](./rules.md#5-the-counters-are-the-contract).

## `t0`, and why alignment is derived

Sample *n* occurs at `t0 + n / samp_rate`, on every channel. Two numbers define the entire grid, so
alignment across TX/RX and across antennas is **derived and assertable** rather than emergent from
scheduling coincidence:

```python
lag = tb.dac_if.samp_time(n) - tb.adc_if.samp_time(n)   # the same for every n
```

`t0` is **owned by the converter and pushed onto the interface at bind**; the sample rate travels the
other way, living on the interface's clock and being *read* by the converter at bind:

```python
def on_rf_bind(self, iface, ep_name):
    iface.set_t0(epoch_for(ep_name), owner=self)   # pushed:  a tile property
    self.samp_rate = iface.samp_rate               # read:    a wire property
```

Setting `t0` from a second owner **raises**, because two declarations that can disagree is the bug.

Two properties fall out of the epoch-plus-rate formulation, and both are the reason for it.

**It handles unequal rates.** ADC and DAC tiles routinely run at different sample rates, so there is
no common event grid to share. A shared metronome event could not express the relationship; `t0` plus
a rate can.

**It is where MTS lives.** Multi-tile synchronization is a bring-up procedure — SYSREF distribution,
tile calibration — and is not a modelable thing. What it *produces* is a fixed, measured offset, and
`t0` is that parameter: per tile, measured at bring-up, zero in simulation.

### What `t0` is not for

It is tempting to buy a pipeline's block latency by staggering the DAC epoch. **Don't** — that models
a tile stagger MTS exists to *prevent*, and it makes correctness depend on a wall-clock margin the
block model does not resolve. The cost belongs to the pipeline, which declares it and gets checked
against the DAC edge's startup underruns.

Aligned grids and a non-zero loop cost are not in tension: alignment is about *when a grid ticks*,
latency is about *which block each tick carries*.

## Reading the counters

```python
adc_if.counters()      # {'blocks_sent': 8, 'blocks_delivered': 8, 'underrun': 0, 'overrun': 0}
adc_if.assert_clean()  # raises unless underrun == 0 and overrun == 0
```

`assert_clean(startup_blocks=L)` allows exactly `L` leading underruns **and checks their grid index**,
so a steady-state fault cannot hide inside a transient's budget and a module that over-declares its
latency fails too.

## Next

- [Connecting the fabric side](./axis_side.md) — the other half of the converter.
- [Block sampling](./sampling.md) — what the metronome is doing underneath all of this.

**Source of truth:** `waveflow/hw/rf_sample_if.py`, `waveflow/simulation/rf_tb.py`,
`tests/hw/test_rf_sample_if.py`.
