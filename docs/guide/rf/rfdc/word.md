---
title: The sample word
parent: Rfdc
grand_parent: RF converters
nav_order: 2
audience: python
api: [RfdcSampWord, samp_per_word, bits_per_samp, bits_per_samp_pack, justify, iq_order, iq_mode]
summary: "Why a converter hands the fabric several samples at a time — the clock ratio makes it unavoidable — and the convention that says which sample lands where. Slot order, the difference between the bits a converter resolves and the bits its slot occupies, where those effective bits sit inside the container, and which of I and Q comes first. All of it is one type, RfdcSampWord, because the rules are AMD's rather than Waveflow's and belong somewhere a reader can see them."
---

# The sample word

Before we describe how to instantiate
an `RfDc` converter, we have to describe how its samples are formatted on the AXI-Stream.  This format is used as an argument in the constructor of `Rfdc`.  This page describes the format options as well as methods to pack and unpack data from that format.

## Sample Packing

A converter and a fabric run at rates that are nowhere near each other. An RFSoC ADC samples at
**one to five giga-samples per second**; the logic behind it runs at **250 to 500 MHz**. That is an
order of magnitude, and it is not going to close.

So the converter cannot hand over one sample per fabric cycle — there aren't enough cycles. It hands
over **several samples in one beat**, and how many is not a design preference:

```
samp_per_word  =  samp_rate / f_axis
```

At 1 GSa/s into a 250 MHz fabric, four. At 2 GSa/s, eight. **Packing is a consequence of the clock
ratio**, and every parameter on this page exists to say precisely *how* those samples are arranged
once you have accepted that there must be more than one.

The number has to come out whole, because a sample cannot straddle a beat. `Rfdc` refuses a
configuration where it does not, rather than rounding.

## Describing Packing Formats with `RfdcSampWord`

In Waveflow, the packing format used by the converter is described by  a **type**, not a handful of parameters.  The base type is `RfdcSampWord`.  An example creation of a type is as follows:

```python
from waveflow.hw.rfdc_samp_word import RfdcSampWord

Word = RfdcSampWord.specialize(samp_per_word=4, bits_per_samp=14, bits_per_samp_pack=16)
Word.bitwidth        # 64  = 4 × 16
Word.samp_type       # FixedField, 14 bits — the QUANTIZER
```

**Anything you omit is inherited from the class you called `specialize` on** — which is what makes
a board preset an ordinary subclass rather than a factory.

| parameter | what it fixes | default |
|---|---|---|
| `samp_per_word` | samples one beat carries — **complex** ones when `iq_mode` | `1` |
| `bits_per_samp` | **effective** bits: what the converter resolves, and the quantizer's precision | `16` |
| `bits_per_samp_pack` | **container** bits: the slot one sample occupies on the bus | `16` |
| `iq_mode` | real samples, or interleaved I/Q | `False` |
| `justify` | where the effective bits sit inside the slot — `"left"` or `"right"` | `"left"` ⚠ |
| `iq_order` | which of I and Q takes the lower slot | `"i_low"` |

⚠ `justify`'s default is [an assumption awaiting a lab measurement](#justify), not a measurement.

Everything else is **derived** — read it, never restate it:

| | is | for |
|---|---|---|
| `bitwidth` | `samp_per_word × bits_per_samp_pack`, doubled for I/Q | the AXI-Stream width |
| `samp_type()` | `FixedField` at `bits_per_samp`, rounding and saturating | quantizing a sample |
| `slot_type()` | signed `IntField` at `bits_per_samp_pack` | what the serializers see |
| `slots_per_word()` | `samp_per_word`, doubled for I/Q | slots in one beat |
| `justify_shift()` | `bits_per_samp_pack - bits_per_samp` when left-justified, else `0` | the one rule below |

These are the converter's *entire* sample geometry: [`Rfdc`](./converter.md) reads them off the type
and declares none of them itself.

`Rfdc` takes the word type and **reads** its geometry off it. You never restate the width; there is
one place it can be wrong.

It is a type rather than three loose numbers for a reason worth stating: **these rules are AMD's, not
Waveflow's.** A different converter family packs differently, and naming the vendor makes that
coupling visible instead of implying the layout is universal.

## Arrays of words

A block of words is an ordinary `DataArray` over the word type — the reason `RfdcSampWord` subclasses
`IntField` rather than inventing a container:

```python
from waveflow.hw.dataschema import DataArray

Block = DataArray.specialize(element_type=Word, max_shape=(64,))
blk = Block()
blk.val            # ndarray, dtype uint64, shape (64,)  — 64 beats = 256 samples
```

`blk.val` is a **numpy array**, not a list of field objects: a `DataArray` over a numpy-backed
element *is* an `ndarray`. Index it, slice it, and hand it to numpy directly.

The choice of `uint64` follows from `Word.bitwidth`. A word wider than 64 bits is stored as
`(n, k)` little-endian `uint64` rows rather than refused — the same
[wide-word convention](../../interface/overview.md) the rest of Waveflow uses.

**In practice you rarely build this array yourself.** Reach for `DataArray` when you need a
*declared* block — a buffer's schema, a bundle written to disk. A block of words *in flight* is a
plain numpy array: it is what `pack` below hands back, and what a stream `get()` hands you.

## Converting samples to words and back

Two functions, and they are exact inverses:

```python
from waveflow.hw.rfdc_samp_word import Rfsoc4x2SampWord, pack, unpack

Word  = Rfsoc4x2SampWord.specialize(samp_per_word=4)
words = pack(Word, stored)      # (n_ch, n_samp) integers -> (n_ch, n_words) uint64
stored = unpack(Word, words)    # and back, exactly
```

You should not need anything else on this page to move samples across the converter's fabric side.
Everything below it is the *why* — the conventions these two implement — and it is there for the
reader checking the model against PG269, not for the one packing a block.

### The shape is `(n_ch, n_samp)`

**Channel-major**, matching the `(n_ch, blksize)` blocks the [RF side](./rf_side.md) already carries,
so there is no transpose at the boundary. One channel is `(1, n_samp)`, not `(n_samp,)` — a 1-D array
is refused rather than promoted, so the pair is an inverse in shape as well as in value.

Each channel is packed **independently** into its own row of words, and **row `ch` is what port
`ch` carries** — the converter presents one AXI-Stream port per channel, so a channel-major array is
exactly a per-port array. (This shape was chosen before that was settled, on the grounds that
interleaving rows afterwards is a separate step while de-interleaving a committed layout is not. It
turned out to be the committed answer; see `plans/adc_model.md`.)

### It takes integers, and that is the interesting part

`pack` takes **stored integers** — what quantization produced — and refuses a float array. Two
questions, two calls, and they are different questions:

```python
stored = from_real(x, Word.samp_type())   # quantize — the CONVERTER's question, at bits_per_samp
words  = pack(Word, stored)               # lay out  — the BUS's question, and lossless
```

A real-valued input would make `pack` **lossy**: quantization happening inside a call whose name says
formatting. That is the one place it must not hide, and it is the
[effective-vs-container confusion](#effective-vs-container) in another hat — so the refusal is the
feature, and the error message names the call you are missing.

Turning words back into amplitudes is the same split run backwards:

```python
x = to_real(array(Word.samp_type(), unpack(Word, words)))
```

The caller therefore knows the amplitude scale. That is right: `full_scale` is a property of the
[converter](./converter.md), not of the word.

### What it refuses

| | |
|---|---|
| `n_samp` not a multiple of `samp_per_word` | **refused, never padded** — the same choice `Rfdc` makes about a non-integer rate |
| a float sample array | refused; quantize first (above) |
| a sample outside `bits_per_samp` | refused — an over-range value shifts into its neighbour's slot and corrupts it silently |
| complex samples into a real word, or the reverse | refused; `iq_mode` is a property of the word |

The first refusal is what buys the second function its signature: because `n_samp` is always a whole
number of words, `n_samp = n_words × samp_per_word` on the way back, and `unpack` needs no length
argument.

### I/Q and wide words

When `iq_mode` is set, `pack` takes a **complex** array of integer-valued samples and routes through
the [slot order](#iq-order) below; `unpack` returns complex. When `Word.bitwidth` exceeds 64 the word
arrays gain the trailing axis of the `(n, k)` wide-word convention — `(n_ch, n_words, k)` — rather
than the word being refused.

### The type is an argument on both sides

`unpack(words)` cannot work. Packed words are a bare `uint64` array, and a stream `get()` hands you
exactly that: no container, no `element_type`, nothing a convention could be recovered from. Having
`pack` return a `DataArray[Word]` would let `unpack` infer the type for arrays that came from `pack`
— and never for the ones that came off a wire, which is the case that matters. Symmetry beats the
shorter signature.

## Slot order: oldest sample, lowest bits {#slot-order}

Samples are packed **time-ascending from the LSBs** — the **oldest** sample in the **least**
significant slot, each in a fixed `bits_per_samp_pack` slot. At 8-bit slots, the samples
`[0, 64, -64, -128]` pack to `0x80c04000`.

**Do not hand-roll this.** Packing goes through the
[generated array serializers](../../vectorization/), never a `.range()` you wrote — and the reason is
sharper than tidiness:

> **Slot order is unobservable at `samp_per_word == 1`.** With one sample per beat there is nothing
> to order, so a slot-order bug passes every test you thought to run — and then fails at four.

That is a standing trap in this repo and it has been paid for more than once.

## Effective bits and container bits are two numbers {#effective-vs-container}

The bits a converter **resolves** and the bits its slot **occupies** are not the same thing:

| | what it is | ZU48DR |
|---|---|---|
| `bits_per_samp` | **effective** — what the converter actually resolves, and the quantizer's precision | **14** |
| `bits_per_samp_pack` | **container** — the width of the slot on the bus | **16** |

They coincide on a part whose resolution happens to match its slot width, and that coincidence is
exactly what makes conflating them dangerous.

**Why it matters, concretely.** `bits_per_samp` sets the quantizer. Take the container width instead
— which is what the *bus* arithmetic tells you — and you get an `ap_fixed<16,1>` quantizer on a 14-bit
converter: a quantisation step **four times finer than the hardware's**, understating the one effect
this model exists to reproduce bit-exactly. A design tuned against that model would be tuned against
a converter that does not exist.

## `justify` — declared, and not yet confirmed {#justify}

If 14 effective bits sit in a 16-bit slot, *where* in the slot?

| | |
|---|---|
| `"left"` | MSB-aligned — the effective bits occupy the high end, low bits zero |
| `"right"` | LSB-aligned — sign-extended into the high bits |

> **The default is `"left"` and it is UNCONFIRMED.** Which one AMD's RFDC uses is a PG269 question
> nobody on this project has answered. It is on the board bring-up list beside the `TVALID` question
> and will be settled in the lab.
>
> `"left"` is the default because MSB alignment makes full scale the same integer whatever the
> converter's resolution, so PL logic need not be re-scaled per part. That is a reason to **expect**
> it — not evidence that it is so.

It is a declared field precisely so the model **states** an answer that hardware can contradict,
rather than assuming one silently. The C++ twin implements both, so a lab answer is a one-field
change, and the tests assert *that the flag exists*, not that it is right.

One consequence that catches people: under MSB alignment the low `16 − 14 = 2` bits of a slot are
**not the converter's**. A test ramp stepping by 1 does not survive quantisation, which is why the RF
examples step by 4 — and why that step, changing, would witness a change in `justify`.

While `bits_per_samp == bits_per_samp_pack`, `justify` is a no-op.

## Real and I/Q {#iq-mode}

`samp_per_word` counts **samples**; `iq_mode` says what a sample *is*:

| `iq_mode` | a sample is | one beat carries | width |
|---|---|---|---|
| `False` | a **real** value | `samp_per_word` real samples | `samp_per_word × bits_per_samp_pack` |
| `True` | a **complex** (I, Q) pair | `samp_per_word` complex samples | `samp_per_word × bits_per_samp_pack × 2` |

A complex sample occupies two slots, so the same count needs twice the bus. An I/Q design fits the
same width by **halving `samp_per_word`**; the information density is identical either way, and the
parameter counts what the design thinks in.

`iq_mode` lives on the **word**, not on the converter, because it is a statement about packing — it
is what makes `bitwidth` follow from the type rather than from a flag elsewhere.

**`iq_mode = True` is not implemented on the converter yet**, and the refusal names what is
missing. Everything *under* it now is:

- the RF-side bundle carries complex blocks and says so in its manifest, and `RFSampIF` declares
  whether its blocks are complex — see [the RF side](./rf_side.md#real-or-complex-blocks);
- `pack` / `unpack` have always handled `iq_mode`, at any channel count;
- the **C++ sample twin** packs and unpacks interleaved I/Q bit-identically to `pack` / `unpack`, in
  both slot orders and at both justifications — so "bit-exact" means the same thing for I/Q that it
  means for real (`tests/build/test_xsi_rfdc_samp.py`).

What is left is the converter itself: the complex paths through its ADC and DAC processes, and its
two C++ models. Until that lands, model I and Q as two real channels (`n_ch = 2`) — which is what
the hardware carries anyway.

A converter checks the two declarations against each other at bind: an `RFSampIF` carrying complex
blocks and a word whose `iq_mode` is `False` are the same fact seen from either side of the
converter, so a disagreement is refused rather than cast away.

### `iq_order` {#iq-order}

Which of I and Q takes the **lower** slot. Like slot order, it is **invisible at
`samp_per_word == 1`**, so it is pinned by a test at two samples per word and nowhere else.

**The declared default is `i_low`, and there is evidence against it.** The quad-tile RFDC's bus is
quoted as `{I1, Q1, I0, Q0}`, which — read in the same convention as the real case, oldest in the
least-significant slot — puts **Q** in the lower one. That is an inference from a community source,
not a measurement, so the default is not being changed on it; it is at the top of the board bring-up
list, above [`justify`](#justify).

It is a one-field change when the lab answers, on both sides: the C++ twin reads the value off
`RfdcFormat` rather than assuming one, and `Rfdc` emits it **by name** (`RFDC_Q_LOW`) into the
generated harness, so a testbench says which order it assumed.

## Next

- [Instantiating the converter](./converter.md) — where this type becomes `Rfdc`'s first argument,
  and the rest of the parameter list.
- [Connecting the fabric side](./axis_side.md) — the rates, the check the converter performs, and
  what that check does *not* cover.

**Source of truth:** `waveflow/hw/rfdc_samp_word.py`, `tests/hw/test_rfdc_samp_word.py`.
