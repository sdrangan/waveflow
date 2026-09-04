---
title: Adding an RF path
parent: Rfdc
grand_parent: RF converters
nav_order: 3
audience: python
api: [Rfdc, RfdcSampWord, Rfsoc4x2SampWord, RFSampIF, StreamIF, Clock, axis_bitwidth]
summary: "What to write when a design needs an RF conversion path. The complete wiring for a receive path in one block — one Rfdc, one RFSampIF on the sample side, one ordinary StreamIF on the fabric side — then the three numbers you have to decide, the word type that carries the rest, the width that is derived for you, and what you can do with the AXI-Stream that comes out: consume it directly, or put a sample buffer behind it."
---

# Adding an RF path

You have a design and it needs to get samples from an ADC, or send them to a DAC. This page is what
to write.

## The whole receive path

```python
from waveflow.hw.rf_sample_if import RFSampIF
from waveflow.hw.interface import StreamIF
from waveflow.hw.rfdc_samp_word import Rfsoc4x2SampWord
from waveflow.simulation.clock import Clock

samp_clk = Clock(freq=256e6)      # the converter's sample rate
axis_clk = Clock(freq=250e6)      # your fabric clock — a different domain

rfdc = Rfdc(name="rfdc", sim=sim, word=Rfsoc4x2SampWord.specialize(samp_per_word=4),
            full_scale=1.0, t0_rx=0.0, t0_tx=0.0)

# --- the RF side: where samples come from -------------------------------
adc_if = RFSampIF(name="adc_if", sim=sim, samp_clk=samp_clk,
                  n_ch=1, blksize=256, n_blk=8)
adc_if.bind("tx", source.rf_ep)          # your RF environment
adc_if.bind("rx", rfdc.rx_rf)

# --- the fabric side: an ORDINARY AXI-Stream ----------------------------
adc_axis = StreamIF(name="adc_axis", sim=sim, clk=axis_clk,
                    bitwidth=rfdc.axis_bitwidth)
adc_axis.bind("master", rfdc.rx_streams[0])   # one AXIS port per channel
adc_axis.bind("slave", my_dut.s_in)
```

That is the whole thing. Transmit is the mirror: bind `my_dut.s_out` to `rfdc.tx_streams[0]`, and
`rfdc.tx_rf` to whatever consumes samples.

**Why the `[0]`.** An `Rfdc` stands for `n_rx` + `n_tx` datapaths, so it presents `n_rx` AXIS master
ports and `n_tx` slave ports, one per channel, while the RF side stays one interface per direction
carrying every channel's row of a block. At `n_rx = 1` there is exactly one port and it is `rx_streams[0]` — indexed even
here, so there is one spelling rather than a special case nobody tests. See
[the endpoints](./converter.md#the-endpoints).

## The three numbers you decide

| | what it is | how to pick it |
|---|---|---|
| `samp_per_word` | samples in one AXI-Stream beat | see below — the arithmetic decides it |
| `samp_clk` freq | the converter's sample rate | the hardware's |
| `axis_clk` freq | your fabric clock | yours |

Everything else about the sample layout — how many bits the converter *resolves*, how wide the slot
each sample rides in is, real or I/Q, and the two packing rules a serializer cannot know — is carried
by the **word type**, and a board preset already states it:
`Rfsoc4x2SampWord.specialize(samp_per_word=4)` is **14 effective bits in a 16-bit slot**, which is
what an RFSoC 4x2's converters are. See
[the sample geometry is one type](./converter.md#the-sample-geometry-is-one-type).

**`samp_per_word` is the one that needs thought**, and the arithmetic decides it rather than taste:

```
samp_rate  =  samp_per_word × f_axis
```

At 1 GSa/s into a 250 MHz fabric that is **4 samples per beat**. Pick it so the division comes out a
whole number — a sample cannot straddle a beat, and the constructor refuses a configuration where it
would.

## The one you do not decide

```python
rfdc.axis_bitwidth        # word.samp_per_word × word.bits_per_samp_pack  (× 2 for I/Q)
```

**Read it off the converter; never restate it.** At four 16-bit slots to a beat that is a 64-bit beat
carrying four samples. Your logic is built against this width, and taking it from the `Rfdc` — which
in turn takes it from its word type — means the three cannot disagree.

You also never tell the `Rfdc` its sample rate. It *reads* it from the `RFSampIF`'s clock when you
bind. Each quantity is declared once, where it physically belongs.

## Only one interface here is new

- **`StreamIF`** — the fabric side. An **ordinary** Waveflow AXI-Stream, wired like any other. Your
  DSP block connects to a converter exactly as it connects to anything else.
- **`RFSampIF`** — the sample side, and the only RF-specific thing on this page. It exists *only in
  simulation*: the converter's RF side is analogue pins, so there is nothing for it to become in RTL.
  It carries a **block** of `(n_ch, blksize)` samples per simulation event, which is why a
  millisecond of signal is simulable at all.

`blksize` is the speed/resolution knob: bigger runs faster and resolves less.

## What you have now, and three things to do with it

Out of each `rfdc.rx_streams[ch]` comes an **ordinary AXI-Stream**, `samp_per_word` samples per
beat, oldest sample in the least-significant slot.

**1. Consume it directly.** Perfectly normal, and the right answer for anything that processes
samples as they arrive — a filter, a detector, a decimator. No buffer, nothing extra to build. Most
designs want this.

**2. Put a finite buffer behind it** (the `RfShotBuf` family: `RfShotTx`, `RfShotRx`) when you need to *hold* samples: capture a window
and read it out afterwards, or play a waveform from memory. Load, then use — nothing reads while
anything writes.

**3. Put a continuous buffer behind it** (the `RfStreamBuf` family: `RfTxStream`, `RfSampBufRx`) when something must read while something
else is still writing — draining a capture while still capturing, or loading the next waveform
mid-play.

If you are not sure, start with (1). A buffer is something you add when you find you need one, and
[choosing a sample buffer](../choosing.md) is the page for that decision.

## Did it work?

A run that finishes tells you very little. Two lines:

```python
adc_if.assert_clean()                                  # nothing lost coming in
assert np.array_equal(captured, to_real(from_real(sent)))
```

**Why the counter and not just the data.** Back-pressure protects you against sending too much and
*nothing* protects you against a converter running dry — it emits well-formed zeros, and a check on
the samples that did arrive still passes. The counter is the only evidence nothing went missing.

## Next

- [Instantiating the converter](./converter.md) — every parameter, and which are baked in at build
- [Connecting the RF side](./rf_side.md) — sources, sinks, and `t0`
- [Connecting the fabric side](./axis_side.md) — packing, I/Q, and the rate check

Once you have something running, two pages worth reading before you trust it:
[what this model cannot tell you](./fidelity.md), and
[the design rules](./rules.md) — seven things that make a converter design wrong.

**Source of truth:** `examples/rf_loopback/`, `tests/examples/test_rf_loopback.py`.
