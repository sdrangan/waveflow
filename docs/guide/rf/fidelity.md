---
title: The fidelity boundary
parent: RF converters
nav_order: 3
audience: python
api: [RFSampIF, StreamIF, offer, dropped, blk_latency, loop_blk_latency]
summary: "What block-level modelling can and cannot tell you. The contract has three conditions — behaviour depends only on sample timestamps, no dependency shorter than two blocks, and the DUT never stalls its input — and only the third is mechanically checkable. It now is, in pysim, as `dropped == 0`. The page also says plainly where that check stops seeing: a consumer that stalls inside a block period is below the model's resolution, demonstrated by a design where RTL loses 72 words and pysim reports none."
---

# The fidelity boundary

Block-level modelling buys speed by moving one block per event instead of one sample. That trade is
exact for some designs and silently wrong for others, and this page is about telling them apart.

## The contract

A block-LT model reproduces the hardware **if all three hold**:

1. **Behaviour depends only on sample *timestamps*, not on arrival times.** A sample's meaning comes
   from its index on the [grid](./sampling.md#t0-is-the-synchronization-primitive), not from when the
   simulator happened to deliver it.
2. **No dependency shorter than `2 × blksize` in sample indices.** One block per converter hop: a
   converter cannot emit samples it has not collected, so a block exists at its grid tick and is
   transmitted across the following period. Two hops — in and out — is two blocks.
3. **The DUT never stalls its input.** A converter cannot be back-pressured, so anything the fabric
   is not ready for is gone.

**Conditions 1 and 2 are assertions you make about your algorithm. Nothing checks them.** No tool
here inspects an algorithm for a short feedback path or for arrival-time sensitivity; if your design
has a sample-rate carrier or timing recovery loop, condition 1 or 2 fails and this modelling style
will mislead you regardless of what any gate says. Most SDR receivers contain at least one such loop.

**Condition 3 is mechanically checkable**, and moving that check into pysim — where it is cheap — is
the point of the machinery below.

## Condition 3, as a number

`StreamIF.dropped` counts words a producer offered that the consumer had no room for:

```python
assert tb.dut.s_in.interface.dropped == 0
```

It stays zero for every ordinary design, because an ordinary module calls `write()`, which *waits*.
Only a producer that physically cannot wait calls
[`offer()`](../interface/stream.md), and only then can the number move. That asymmetry is deliberate:
"who is willing to wait" is a property of the **producer**, not of the wire — the same AXI-Stream
carries both.

Two things had to be corrected before the count could mean anything:

- **The transfer is paced by the producer's rate.** Charging a converter's 64-word block at the
  fabric clock claimed it crossed in 213 ns when the converter takes 1000 ns to produce it — handing
  the consumer a 787 ns hole to drain in that the hardware never gives it.
- **The boundary depth is 2.** A testbench declared 128 on the AXIS interfaces; a top-level argument
  cannot carry a FIFO depth at all. Vitis ignores it (`HLS 214-387`), and in one pragma placement
  says nothing while doing so. `composite_top_spec` now refuses the declaration, because a depth that
  is silently 2 is worse than no depth — the number in the Python reads like a fact.

## Where the check stops seeing {#the-resolution-limit}

Here is a design that violates condition 3, where **RTL loses 72 of 512 words and pysim reports
none**. Both are behaving correctly.

`RfSampPassThrough` reads a whole 64-word block and only then writes it. Per 1000 ns block period it
needs about 213 ns of work, so **at block granularity it comfortably keeps up** — it is back at its
input long before the next block starts, and that is exactly what pysim measures. At RTL the words
are not a block; they arrive one per ~4.7 cycles, spread across the whole period, and the DUT's write
phase is a *contiguous* 213 ns during which it accepts nothing. About 13.6 words arrive into a 2-deep
FIFO. The rest are gone.

The loss is a **phase** effect inside one block period. Block-LT carries one event per block, so the
information simply is not there. This is not a missing feature; it is the granularity boundary, and
it is worth stating in the form a designer can use:

> `dropped == 0` in pysim means *the consumer keeps up block for block*. It does not mean the
> consumer is ready continuously **within** a block, and a converter needs the latter.

So condition 3 has a coarse half and a fine half. pysim checks the coarse half for free, on every
run, with no toolchain. The fine half needs RTL.

### Why not just make pysim stricter

Three admission rules were measured against a consumer that **never stalls** — a design that
satisfies condition 3 by construction:

| rule | this design | never-stalling consumer |
|---|---|---|
| clip a burst to the free space | 496 dropped | **504 dropped** |
| refuse when full, sampled before the instant settles | 496 | **256** |
| refuse when full, after the instant settles | 0 | 0 |

The first two report heavy loss for a correct design, which makes `dropped == 0` unreachable and the
clause worthless. They fail for a structural reason: this framework has never treated "a 64-word
burst through a depth-2 stream" as a violation — `_push_to_endpoint` routes intra-burst overflow to
an unbounded overflow counter — because depth models a consumer *hiccup between bursts*, not
intra-burst capacity. A rule that fires on every design is not a stricter check; it is a broken one.

The shipped rule has no false positives and no false negatives *at block granularity*: zero for a
consumer that never stalls, and rising with the stall for one that cannot keep up.

## What the two backends count

Same scenario, same graph, and the numbers do not match. They are not meant to:

| | pysim | XSI |
|---|---|---|
| where loss is accounted | the `RFSampIF` edge, and `StreamIF.dropped` | the converter models and the channel |
| units | whole **blocks**, and **words** at the fabric boundary | **words** (ADC drop), **cycles** (DAC underrun), **blocks** (channel) |
| ADC→fabric loss | 0 — sub-block, below the resolution | 72 of 512 words |
| startup transient | 2 blocks | 1 block |

The startup difference is pacing, not physics: pysim paces the RF side on the **edge**'s metronome,
XSI on the **source**, so the RTL ADC has its first block at *t=0* and pysim's does not.

None of this should be "fixed" by making one side match the other. The disagreement is the data.

## What is *not* a divergence

The DAC's startup zero-fill **is** physics, on both backends. A DAC plays on its tile clock and
underflows when starved; there is no protocol signal for "you were late", so it emits zeros and
counts. An earlier session saw the RTL model emit nothing at startup, concluded pysim's metronome was
an artifact, and deleted the check. That was backwards — the RTL model was emitting on buffer
fullness, which is not what a converter does. With it corrected, both backends show the transient and
`blk_latency` is checkable on both.

The lesson generalises: when the two backends disagree, the question is *which one is modelling the
hardware*, not which one is more convenient.

## See also

- [Block sampling](./sampling.md) — the grid, and why the metronome is on the edge.
- [The converter](./converter.md) — the AXIS side and the underflow/overflow contract.
- [RF loopback](../../examples/rf_loopback/) — the worked example these numbers come from.

**Source of truth:** `waveflow/hw/interface.py` (`offer`, `dropped`),
`tests/hw/test_stream_offer.py`, `tests/examples/test_rf_loopback_xsi.py`.
