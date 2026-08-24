---
title: Real and I/Q
parent: Rfdc
grand_parent: RF converters
nav_order: 2
audience: python
api: [iq_mode, complex_samp, RfdcSampWord, RFSampIF, rf_samples]
summary: "What a sample on a channel is. Complex-ness is a property of the word rather than of the port structure, so an I/Q design gets a wider sample and not twice as many ports. The RF environment is complex baseband throughout, which turns the RFDC's digital up/down-converter from a piece of DSP into a choice of representation — and which is why the converter carries two independent flags whose relationship is an implication rather than an equality."
---

# Real and I/Q

[Channels](./channels.md) is about how many channels a converter has. This page is about what a
sample on one of them **is** — real or complex — which is answered in two different places, and the
relationship between them is the thing on this page worth reading twice.

## Complex-ness is a property of the word

An I/Q design does **not** get twice as many ports. It gets a wider *sample*:

```python
real = Rfsoc4x2SampWord.specialize(samp_per_word=4)                  # 4 real samples,    64 bits
iq   = Rfsoc4x2SampWord.specialize(samp_per_word=2, iq_mode=True)    # 2 complex samples, 64 bits
```

Both are 64-bit words on the same bus; an I/Q design fits by **halving `samp_per_word`**, because a
complex sample occupies two slots. The port count never moves.

That is a deliberate modelling choice, and the alternative is worth naming. The IP's *wiring* for an
I/Q datapath can be two streams — on a dual-tile (Gen 1) part, I and Q come out of `m00_axis` and
`m01_axis` separately. Mirroring that into the model would make every consumer downstream — the
buffers, the BFM, your logic — learn a port-pairing rule in order to say "complex". Carrying
complex-ness as a **type** keeps it a data question, which is where it belongs. On the **quad-tile**
parts this project targets (ZU48DR / RFSoC 4x2) the hardware interleaves I and Q on one bus anyway,
so the model's port is bit-identical to the IP's.

## The RF environment is complex baseband

Every signal on the RF side is modelled in **complex baseband**, including one that is physically a
real passband waveform: a real passband signal *is* its complex envelope, and the envelope is what the
model carries.

This is the observation that removes a whole subsystem. The RFDC's digital up- and down-converter —
NCO, complex mixer, the thing that turns I/Q into a real IF — becomes, in this representation:

```
(a, b)  ->  a + ib
```

Forming a complex number. Not multiplying by a carrier. **No NCO, no complex multiply, and no
equivalence obligation** — there is no arithmetic here that a C++ twin would have to be proven
bit-exact against.

Two consequences follow, and the second is the useful one:

- **I/Q → real and I/Q → I/Q are indistinguishable from the RF side.** Both present one complex
  baseband channel. Whether the quadrature mixing happens on-chip or in an external analog modulator
  is a fact about the analog domain *past the model's boundary*.
- **Where the conversion happens becomes a modelling choice**, and the model has to be able to say
  which. That is what the second flag is for.

## The two flags {#the-two-flags}

| | lives on | asks |
|---|---|---|
| `word.iq_mode` | the [sample word](./word.md) | **bus packing** — does one beat carry interleaved I/Q? |
| `RFSampIF.complex_samp` | the [RF edge](./rf_side.md) | **signal representation** — does this edge carry a complex baseband envelope? |

They are genuinely different questions, and three of their four combinations are legal:

| `iq_mode` | `complex_samp` | what it means |
|---|---|---|
| `0` | `0` | **real baseband end to end** — direct sampling, no conversion anywhere. Every example in this repo today. |
| `0` | `1` | **the DUC/DDC is in the RF domain**, outside the converter. The edge is complex-*typed* because the environment uniformly is; its content is real. |
| `1` | `1` | **the DUC/DDC is in the converter** — interleaved I/Q beats, complex blocks. |
| `1` | `0` | **refused.** The beats carry a Q and the edge has nowhere to put it. |

The rule is an **implication, not an equality**:

```
word.iq_mode  ⇒  complex_samp
```

and the reason is one sentence: **the converter performs no I/Q mapping, so it can never create a Q.**
A complex word needs a complex edge. A real word is fine on either.

### The `(0, 1)` row, and the guard on it

This is the row that needs a moment. A real converter on a complex-baseband edge is what you have
whenever the down-conversion ran *upstream* of the ADC, or the up-conversion runs *downstream* of the
DAC. The edge is complex-typed because the RF environment is uniformly complex baseband; the content
is real, `x + j0`, because the conversion happened elsewhere. Taking the real part is then **exact**.

`Rfdc.rf_samples()` does that, and **checks the "then"** rather than assuming it:

```python
if np.any(arr.imag):
    raise ValueError(... "carries a non-zero Q, but the AXIS word is real" ...)
```

A live Q on such an edge is not a representation detail. A real converter cannot carry it, and
dropping it would hand the fabric a block of the right shape and length holding half a signal — which
is exactly the class of failure the RF path is built to make impossible, arriving by a side door.

### Two real channels into one complex signal

A design that drives an **external quadrature modulator** from two *real* DAC channels needs
`(a, b) → a + ib` across two channels, which halves the channel count. That belongs to a separate
pysim-only RF block, outside the converter — the same discipline that keeps gain and multipath in
`Channel` rather than in the edge. It keeps `n_ch = n_rx = n_tx` on the converter and its two sides
symmetric.

**That block does not exist yet.** Nothing has needed it; it is a handful of lines when something
does.

## What the converter checks, and when

At **bind**, `Rfdc` reads what it does not own and refuses what cannot work:

- the RF edge's `n_ch` must equal `n_rx` / `n_tx` — see [channels](./channels.md#one-number-named-for-its-object);
- `word.iq_mode ⇒ complex_samp`, above.

That second check is the same rule [`RFSampIF.put()`](./rf_side.md#real-or-complex-blocks) already
applies one level down — it refuses a complex block on a real edge and **widens** a real block on a
complex one. An equality check here would be stricter than the interface it guards.

At **`pre_sim`**, once the clocks are bound: `samp_rate <= samp_per_word · f_axis`, which is the
port's capacity and not your design's — see [rule 4](./rules.md#4-port-capacity-is-not-design-capacity).

## Next

- [The sample word](./word.md) — how `iq_mode` sits beside slot order, justification and `iq_order`.
- [The RF side](./rf_side.md) — where `complex_samp` is declared, and the widening rule.
- [Fidelity](./fidelity.md) — what this model does *not* tell you.

**Source of truth:** `examples/rf_loopback/rfdc.py`, `waveflow/hw/rf_sample_if.py`,
`waveflow/hw/rfdc_samp_word.py`, `plans/adc_model.md` § *Channels, ports, and where I/Q lives*.
