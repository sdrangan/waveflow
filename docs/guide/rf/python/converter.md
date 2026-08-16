---
title: Instantiating the converter
parent: Python
grand_parent: RF converters
nav_order: 2
audience: python
api: [Rfdc, RFSampIFRx, RFSampIFTx, StreamIFMaster, StreamIFSlave, FixedField, axis_bitwidth]
summary: "How to create an Rfdc: the full parameter list and which kind each parameter is, the four endpoints and which two cross the cut, what the constructor refuses and why, and the bit-exact quantization the model does. The how-to page — the contracts it implies are in the design rules."
---

# Instantiating the converter

[`Rfdc`](../../../../examples/rf_loopback/rfdc.py) is **one module carrying both directions**. Not
two blocks: the TX and RX sample counters must hold a fixed relation, and that is a property *of the
converter*, which is also what lets the two grids' time origins have exactly one owner.

```python
rfdc = Rfdc(name="rfdc", sim=sim, nbits=16, samp_per_word=4, full_scale=1.0,
            t0_rx=0.0, t0_tx=0.0)
```

That is the whole call. Everything else it needs — the sample rate, the block size — it *reads* off
the interfaces you bind it to.

## The four endpoints

| path | RF side | fabric side |
|---|---|---|
| **ADC** | `rx_rf` — `RFSampIFRx`, blocks in from the environment | `rx_stream` — `StreamIFMaster`, AXI-Stream out to the PL |
| **DAC** | `tx_rf` — `RFSampIFTx`, blocks out to the environment | `tx_stream` — `StreamIFSlave`, AXI-Stream in from the PL |

The stream endpoints **cross the [cut](../../flows/modules.md#the-cut)**; the RF endpoints do not.
That split is the whole shape of the thing: it is why the converter's C++ realization is what it is,
and why an `RFSampIF` has no RTL counterpart to write.

The four endpoints exist whether or not a path is used. A receive-only design sets `n_tx=0` and the
DAC endpoints simply stay unbound — no rate check, no process, no model — which costs nothing and
keeps the endpoint set a property of the class rather than of a particular build. Wiring a fake DAC
in to satisfy the model would put a metronome in the design that nothing feeds, inventing underruns
to report.

## The parameters, and which kind each is

| parameter | kind | default | what it does |
|---|---|---|---|
| `n_rx`, `n_tx` | `HwParam[int]` | 1 | RF channels per direction on the AXIS side |
| `nbits` | `HwParam[int]` | 16 | converter resolution — the width of one sample on the wire |
| `iq_mode` | `HwParam[int]` | 0 | `0` real, `1` interleaved I/Q (doubles the bits per sample slot) |
| `samp_per_word` | `HwParam[int]` | 4 | samples per AXIS word — the **structural** integer |
| `full_scale` | plain field | 1.0 | the amplitude reference quantization is relative to |
| `t0_rx`, `t0_tx` | plain field | 0.0 | when each tile's sample counter starts |

**The `HwParam` rows set the word layout synthesized logic is built against.** `samp_per_word` is on
that list because a sample cannot straddle a slot: port width is `samp_per_word · nbits` (×2 for
interleaved I/Q), and `rfdc.axis_bitwidth` is the number to hand your DUT.

**`samp_rate` is deliberately not a parameter.** It lives on the RF interface's clock and the
converter reads it at bind; `t0` travels the other way and is *pushed* onto the interface. Each
quantity is declared once, where it physically belongs. Two declarations that can disagree is the bug
both directions exist to avoid.

**There is no `spc`.** `samp_per_word` is the structural integer; everything else at this boundary is
a rate *ratio* — derived, and generally fractional. See
[connecting the fabric side](./axis_side.md#there-is-no-spc).

> **`full_scale` is not a `DynParam`, and the distinction is finer than it looks.** `DynParam` does
> not mean "binds at init"; it means **emitted as a member assignment** — `<model>.<field> = <expr>;`.
> This value's C++ realization is a *constructor argument*, riding inside the `RfdcFormat` literal
> the generated models take, so tagging it would emit an assignment to a member that does not exist.
> Zero would be doubly wrong — meaningless as an amplitude reference *and* falsy, which
> `discover_dyn_params` skips — so the constructor refuses it either way.

## What the constructor refuses

Each of these is refused loudly at construction rather than reported later as a symptom.

| condition | why it raises |
|---|---|
| `n_rx > 1` or `n_tx > 1` | whether >1 channel is one AXIS port per channel or one wide port is an open question; it decides how many BFM duals a testbench needs, so it is not settled by default |
| `iq_mode != 0` | interleaved I/Q needs the complex bundle format |
| `full_scale <= 0` | see the note above |
| `samp_per_word · nbits > 64` | wider than the stream word |

`n_rx = 0` (or `n_tx = 0`) is **not** an error — that is a receive-only or transmit-only tile, and it
is the configuration a capture design uses.

One more check happens later, at `pre_sim`, because it needs the bound clocks:
`samp_rate <= samp_per_word · f_axis`. That is the **port's** capacity and not your design's — see
[rule 4](./rules.md#4-port-capacity-is-not-design-capacity).

## Bit-exact quantization

"Evaluate the effect of bit widths in Python" only means something if the Python does what the
hardware will. So the converter quantizes through the integer-backed `FixedField`:

```python
self.SampType = FixedField.specialize(nbits, 1, signed=True,
                                      q_mode=QMode.AP_RND, o_mode=OMode.AP_SAT)
```

Quantization is `ap_fixed<nbits,1>` with `AP_RND` + `AP_SAT`: `floor(x/full_scale · 2^(nbits-1) +
0.5)`, clamped. `AP_RND` is round-half-**up**, not half-even and not half-away — `-0.5` stored units
rounds to `0`. A converter clips; it does not wrap.

The other half of the contract, [packing](./axis_side.md#the-packing-contract), is a property of the
port rather than of the converter, and lives on the fabric-side page.

Both are checked against their C++ twins by
[a conformance test](../../../../tests/build/test_xsi_rfdc_samp.py) rather than against a re-reading
of the spec, and the end-to-end consequence is checked where it matters: a block that goes Python →
quantize → pack → real RTL → unpack → dequantize → Python comes back **bit-identical**.

## Next

- [Connecting the RF side](./rf_side.md) — the interface, the sources and sinks, and `t0`.
- [Connecting the fabric side](./axis_side.md) — packing, `samp_per_word`, and the rate check.

**Source of truth:** `examples/rf_loopback/rfdc.py`, `tests/examples/test_rf_loopback.py`.
