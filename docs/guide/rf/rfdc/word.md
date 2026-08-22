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

**In practice you rarely build this array yourself.** The conversion helpers below return one, and
the converter's stream endpoints produce and consume them. Reach for `DataArray` when you need a
*declared* block — a buffer's schema, a bundle written to disk — rather than a value in flight.

## Conversion methods

[I think before we walk through the details for how the packing is done, we should just give the user python methods to pack and unpack arrays of (nsamp, nch) samples to RfdcSampWord arrays.  I assume we have them.  The following section is likely not needed by most users, since the packing and unpacking are already handled.]

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

**`iq_mode = True` is not implemented yet.** The converter refuses it rather than half-supporting it:
the RF-side bundle format is float64 with no manifest field for complex, and the quantizer's
conformance twin covers real `FixedField` only. Until then, model I and Q as two real channels
(`n_ch = 2`) — which is what the hardware carries anyway.

### `iq_order` {#iq-order}

Which of I and Q takes the **lower** slot. Like slot order, it is **invisible at
`samp_per_word == 1`**, so it is pinned by a test at two samples per word and nowhere else.

## Next

- [Instantiating the converter](./converter.md) — where this type becomes `Rfdc`'s first argument,
  and the rest of the parameter list.
- [Connecting the fabric side](./axis_side.md) — the rates, the check the converter performs, and
  what that check does *not* cover.

**Source of truth:** `waveflow/hw/rfdc_samp_word.py`, `tests/hw/test_rfdc_samp_word.py`.
