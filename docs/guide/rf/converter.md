---
title: The converter
parent: RF converters
nav_order: 2
audience: python
api: [Rfdc, RfdcAdcMaster, RfdcDacSlave, RfdcFormat, BfmModel, bfm_model, words_per_cycle, RfFileSource, RfFileSink]
summary: "The Rfdc module and its two RTL-side models: the AXI-Stream packing contract, samp_per_word versus the two derived rate conversions, bit-exact quantization through FixedField, and the underflow/overflow contract — backpressure protects against over-production and nothing protects against under-production, so the counters are the gate. Written from a working RTL run, including what that run found."
---

# The converter

[`Rfdc`](../../../examples/rf_loopback/rfdc.py) is one module carrying **both** directions. Not two
blocks, because the TX and RX sample counters must hold a fixed relation and that is a property *of
the converter*; see [`t0`](./sampling.md#t0-is-the-synchronization-primitive).

It has four endpoints in two pairs:

| path | RF side | fabric side |
|---|---|---|
| **ADC** | `rx_rf` — blocks in from the environment | `rx_stream` — AXI-Stream out to the PL |
| **DAC** | `tx_rf` — blocks out to the environment | `tx_stream` — AXI-Stream in from the PL |

The stream endpoints **cross the cut**; the RF endpoints do not. That split is the whole shape of the
thing, and it is why the converter's C++ realization is what it is.

## One object per data path, spanning the cut

The ADC path is **one** model that binds RTL pins on the fabric side *and* a
[behavioral edge](../interface/behavioral.md) on the RF side:

```python
def bfm_model(self):
    return (BfmModel("RfdcAdcMaster", ports=("rx_stream", "rx_rf"), extra_args=(...)),
            BfmModel("RfdcDacSlave",  ports=("tx_stream", "tx_rf"), extra_args=(...)))
```

Two declarations, not one, and each port resolves by its own kind — see
[a module may declare more than one model](../comp_codegen/xsi_tb.md#per-port). Two facts force it:

- **the class is per path** — the ADC needs `RfdcAdcMaster`, the DAC `RfdcDacSlave`, and one
  declaration per module cannot say both;
- **the constructor shape is per port** — a boundary port contributes `sim.dut(), <ns>::<port>`, an
  edge port contributes a channel variable.

What it emits:

```cpp
RfdcAdcMaster s_in (sim.dut(), rf_pass_through_ports::s_in,  adc_if, RfdcFormat{16, 4, 1.0}, 0.213…);
RfdcDacSlave  s_out(sim.dut(), rf_pass_through_ports::s_out, dac_if, RfdcFormat{16, 4, 1.0}, 0.213…, 256);
```

> **`RfdcFormat` is a literal, not an identifier.** The harness promotes any bare identifier in
> `extra_args` to a `Harness(...)` parameter typed `const std::vector<uint64_t>&` — which an
> `RfdcFormat` is not. A literal needs no generator change.

## There is no `spc` — there are two derived rate conversions

`samp_per_word` is the **structural integer**: a sample cannot straddle a slot, so the port is
`samp_per_word × nbits` bits wide. Everything else at this boundary is a *ratio*, derived and
generally fractional:

| boundary | conversion | lives in |
|---|---|---|
| AXIS ↔ fabric | `samp_rate / (samp_per_word × f_axis)` words per cycle | the converter models (`RateTick`) |
| RF ↔ fabric | `samp_rate / (blksize × f_axis)` blocks per cycle | *implied* — see below |

**Derived, never declared.** Both terms already exist: `samp_rate` on the RF interface's clock,
`f_axis` on the AXIS interface's clock. The converter *reads* both rather than restating either, so
there is no third statement that could disagree. At 256 MSa/s into 300 MHz with four samples per
word that is `0.2133…` words per cycle — a number no integer expresses, which is why the model
carries a fractional-credit accumulator rather than a count.

The second conversion turns out **not to need its own object**. The block cadence follows from the
word rate: the ADC pulls a block when it has consumed the previous one's words, so the RF grid
emerges from `words_per_cycle` and the channel's depth bounds how far the source may run ahead. One
mechanism, one place.

## Bit-exact quantization

"Evaluate the effect of bit widths in Python" only means something if the Python does what the
hardware will. So both sides implement the same two contracts, and a
[conformance twin](../../../tests/build/test_xsi_rfdc_samp.py) checks them against each other rather
than against a re-reading of the spec:

- **Quantization** is `ap_fixed<nbits,1>` with `AP_RND` + `AP_SAT`: `floor(x/full_scale · 2^(nbits-1)
  + 0.5)`, clamped. `AP_RND` is round-half-**up**, not half-even and not half-away — `-0.5` stored
  units rounds to `0`. A converter clips; it does not wrap.
- **Packing** is time-ascending from the LSBs, each sample in a fixed `nbits` slot. Samples
  `[0, 64, -64, -128]` at `nbits=8` pack to `0x80c04000` — the **oldest** sample in the **least**
  significant slot.

`samp_per_word == 1` hides slot-order bugs entirely (order is unobservable with one sample per
word), so a packing sweep needs both ends.

The end-to-end consequence is checked where it matters: a block that goes Python → quantize → pack →
real RTL → unpack → dequantize → Python comes back **bit-identical**.

## The underflow/overflow contract

There is a real asymmetry in the hardware, and the two sides of it are not symmetric in the model
either:

| | signalled? | what the model does |
|---|---|---|
| the fabric is not ready for an ADC beat | no — the ADC presents it regardless | **drop**, and count |
| a DAC beat was due and none came | no — AXI-Stream cannot say "you were late" | count the cycle |
| the fabric is not ready for a DAC beat | n/a — a converter is always ready | — |

This is why `RfdcAdcMaster` is not an `AxisMaster` and `RfdcDacSlave` is not an `AxisSlave`. The
data would be the same; the **protocol behaviour** differs, in the one direction that matters. A
generic model *blocks*, and blocking hides exactly the failure a converter design must not have.

**The counters are the contract.** They are also the only evidence: a design that dropped a quarter
of its samples still finishes, still produces well-formed output, and still passes every functional
check on the data that did arrive.

### What that caught, on the first real run {#the-drop-finding}

The RF loopback's digital logic reads a whole block before writing it, so `TREADY` is low for ~64
cycles at a stretch while the converter presents a beat every ~4.7 cycles regardless. Over eight
blocks:

- the ADC produces **512** words and the fabric accepts **440**;
- **72 are dropped**;
- the first block is still bit-identical, and everything after the first write phase is not.

pysim does not show this either, but no longer for the reason first recorded. Its stream master now
`offer()`s rather than `write()`s, at the converter's own rate and against the real 2-deep boundary
— and still reports zero, because at **block** granularity this DUT genuinely keeps up (213 ns of
work per 1000 ns period). The loss is a phase effect *inside* a block period, which is below what
block-LT can resolve. See [the fidelity boundary](./fidelity.md#the-resolution-limit).

That is a design shortfall (the fix is to overlap the read and the write, i.e. two tasks and a
channel), not a modelling error, and it is recorded as a gate rather than smoothed over.

## The RF side at RTL

The environment beyond the edges is file-backed, exactly as in pysim: an `RfFileSource` loads the
bundle in `pre_sim` and an `RfFileSink` dumps its capture in `post_sim`, so a loopback is a
file-to-file byte comparison in either backend. **Bundle I/O lives on the nodes**; the channel
carries no file machinery at all.

Those two, the block message and the channel alias live in `xsi_rf_block.h`, which depends on nothing
but the standard library — an edge and its peers bind no RTL pins, so they compile and are gated
under a plain `g++`.

## The two backends count different things

Same scenario, same graph, and they do not line up:

| | pysim | XSI |
|---|---|---|
| where | the `RFSampIF` **edge** | the converter models **and** the channel |
| units | whole **blocks** | **words** (ADC drop), **cycles** (DAC underrun), **blocks** (channel) |
| ADC→fabric loss | none — the master *blocks* | dropped, and counted |

Neither is wrong, and neither is being redefined to make them agree: that mapping is the input to a
cross-backend equivalence harness (`plans/behavioral_edges.md` S4) which does not exist yet, and
flattening the difference now would destroy the information it needs.

## See also

- [Block sampling](./sampling.md) — the edge the RF side rides on.
- [RF loopback](../../examples/rf_loopback/) — the worked example, in both backends.
- [Behavioral edges](../interface/behavioral.md) — authoring the channel.

**Source of truth:** `examples/rf_loopback/rfdc.py`, `waveflow/build/xsi/xsi_rfdc.h`,
`waveflow/build/xsi/xsi_rf_block.h`, `tests/examples/test_rf_loopback_xsi.py`.
