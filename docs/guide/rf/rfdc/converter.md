---
title: Instantiating the converter
parent: Rfdc
grand_parent: RF converters
nav_order: 3
audience: python
api: [Rfdc, RfdcSampWord, Rfsoc4x2SampWord, RFSampIFRx, RFSampIFTx, StreamIFMaster, StreamIFSlave, FixedField, axis_bitwidth]
summary: "How to create an Rfdc: the word type that carries its sample geometry, the full parameter list and which kind each parameter is, the four endpoints and which two cross the cut, what the constructor refuses and why, and the bit-exact quantization the model does. The how-to page — the contracts it implies are in the design rules."
---

# Instantiating the converter

[`Rfdc`](../../../../examples/rf_loopback/rfdc.py) is **one module carrying both directions**. Not
two blocks: the TX and RX sample counters must hold a fixed relation, and that is a property *of the
converter*, which is also what lets the two grids' time origins have exactly one owner.

```python
rfdc = Rfdc(name="rfdc", sim=sim, word=Rfsoc4x2SampWord.specialize(samp_per_word=4),
            full_scale=1.0, t0_rx=0.0, t0_tx=0.0)
```

That is the whole call. Everything else it needs — the sample rate, the block size — it *reads* off
the interfaces you bind it to.

## The sample geometry is one type

`word` is the [`RfdcSampWord`](./word.md) subclass built on the previous page, and it is the only
place this converter's sample geometry is stated. It replaced three loose parameters — `nbits`,
`samp_per_word`, `iq_mode` — and there is deliberately **no convenience path back**: keeping either
name beside the type would be a second source of truth for the same geometry.

`Rfdc` *reads* `bitwidth`, `samp_per_word` and `bits_per_samp` off the type. You never restate any
of them, so there is one place each can be wrong.

`Rfsoc4x2SampWord.specialize(samp_per_word=4)` is the board preset — **14-in-16**, 14 effective bits
in a 16-bit slot, which is what a ZU48DR actually is — asking only for the beat width the design
wants. Two of the fields it carries are rules **a serializer cannot know**, and one of those,
[`justify`](./word.md#justify), is an assumption awaiting a lab measurement rather than a
measurement.

## The endpoints — one RF interface per direction, one AXIS port per channel {#the-endpoints}

| path | RF side | fabric side |
|---|---|---|
| **ADC** | `rx_rf` — `RFSampIFRx`, one interface carrying every receive channel's row of a block | `rx_stream_0 .. rx_stream_{n_rx-1}` — `StreamIFMaster`, one AXI-Stream out to the PL **per channel** |
| **DAC** | `tx_rf` — `RFSampIFTx`, likewise for transmit | `tx_stream_0 .. tx_stream_{n_tx-1}` — `StreamIFSlave`, one in from the PL per channel |

`rfdc.rx_streams` / `rfdc.tx_streams` are the same objects as a list, in channel order — so wiring is
`rfdc.rx_streams[ch]`. The indexed attributes exist because a `BfmModel` names endpoints by
*attribute*, and a subscript is not an attribute name.

**An `Rfdc` is a tile.** One of them stands for `n_rx` receive and `n_tx` transmit datapaths, and the
two sides deliberately take different shapes, each the one its consumer wants:

- the **RF** side wants the channels *together* — a block is a block, and splitting it per channel
  would give `n_ch` events per block period against the whole point of block-LT;
- the **AXIS** side wants them *apart* — that is what the IP presents, and one port per stream is
  what keeps your DUT's ports identical across pysim, XSI and a bitstream.

A single wide interleaved port was considered and rejected: it would move a vendor packing rule into
every design that touches a converter.

Row `ch` of what [`pack`](./word.md) returns is what port `ch` carries, so a channel-major array
*is* a per-port array and nothing at this boundary transposes anything.

**Indexed even at one channel.** `rx_stream` without a suffix would be a second spelling that exists
only at `n_ch == 1`, so every consumer would carry the special case — and the one-channel path would
be the only one anybody tested.

The stream endpoints **cross the [cut](../../flows/modules.md#the-cut)**; the RF endpoints do not.
That split is the whole shape of the thing: it is why the converter's C++ realization is what it is,
and why an `RFSampIF` has no RTL counterpart to write.

The endpoints exist whether or not a path is used. A receive-only design sets `n_tx=0` and the
DAC endpoints simply stay unbound — no rate check, no process, no model — which costs nothing and
keeps the endpoint set a property of the class rather than of a particular build. Wiring a fake DAC
in to satisfy the model would put a metronome in the design that nothing feeds, inventing underruns
to report.

## The parameters, and which kind each is

| parameter | kind | default | what it does |
|---|---|---|---|
| `n_rx`, `n_tx` | `HwParam[int]` | 1 | RF channels per direction — **and AXIS ports**, because they are the same number in both `iq_mode` settings |
| `word` | plain field | 4 × 16-bit | the sample geometry, as a type — see above |
| `full_scale` | plain field | 1.0 | the amplitude reference quantization is relative to |
| `t0_rx`, `t0_tx` | plain field | 0.0 | when each tile's sample counter starts |

**`word` is a plain field for a mechanical reason, not a judgement.** `HwModule.__post_init__` wraps
every `HwParam` value in `HwParamValue(int(value))`, so a *type*-valued parameter cannot be one.
Nothing is lost: an `Rfdc` declares no `kernel_task`, so none of its parameters ever reached a
template argument — they were build-time structure for the **models**, which read them off the word.

`rfdc.axis_bitwidth` is the number to hand your DUT, and it is *read* off the word rather than
restated: `samp_per_word · bits_per_samp_pack`, ×2 for interleaved I/Q.

**`samp_rate` is deliberately not a parameter.** It lives on the RF interface's clock and the
converter reads it at bind; `t0` travels the other way and is *pushed* onto the interface. Each
quantity is declared once, where it physically belongs. Two declarations that can disagree is the bug
both directions exist to avoid.

**There is no `spc`.** `word.samp_per_word` is the structural integer; everything else at this boundary is
a rate *ratio* — derived, and generally fractional. See
[connecting the fabric side](./axis_side.md#there-is-no-samples-per-cycle-parameter).

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
| `word` is not an `RfdcSampWord` | it is a type, not a width — a bare `64` is the mistake this catches |
| `word.iq_mode` | the **word** can already say "interleaved I/Q", and the RF bundle can now carry complex blocks ([the RF side](./rf_side.md#real-or-complex-blocks)); what is still missing is the quantizer's complex conformance twin |
| `full_scale <= 0` | see the note above |
| `word.bitwidth > 64` | wider than the stream word |

`n_rx = 0` (or `n_tx = 0`) is **not** an error — that is a receive-only or transmit-only tile, and it
is the configuration a capture design uses. Neither is `n_rx > 1`: the count is one number with one
meaning, so there is no mode/port-count agreement left to check. What *is* checked, at bind, is that
the RF interface's `n_ch` equals it — the same quantity stated twice, and a disagreement is one of
the two declarations being wrong rather than something to broadcast over.

The bind-time check has a second half, and it is the one that spans the converter: the RF edge's
`complex_samp` must agree with `word.iq_mode`. What a block on the RF side holds and how a complex
sample sits inside an AXIS beat are the same fact seen from either side, and the converter is the
only object that sees both.

One more check happens later, at `pre_sim`, because it needs the bound clocks:
`samp_rate <= word.samp_per_word · f_axis`. That is the **port's** capacity and not your design's — see
[rule 4](./rules.md#4-port-capacity-is-not-design-capacity).

## Bit-exact quantization

"Evaluate the effect of bit widths in Python" only means something if the Python does what the
hardware will. So the converter quantizes through the integer-backed `FixedField`:

```python
self.SampType = self.word.samp_type()     # ap_fixed<bits_per_samp, 1, AP_RND, AP_SAT>
```

Quantization is `ap_fixed<bits_per_samp,1>` with `AP_RND` + `AP_SAT`:
`floor(x/full_scale · 2^(bits_per_samp-1) + 0.5)`, clamped. `AP_RND` is round-half-**up**, not
half-even and not half-away — `-0.5` stored units rounds to `0`. A converter clips; it does not wrap.

**The width is `bits_per_samp`, the effective count — never the container.** A 14-bit converter on a
16-bit bus quantizes to 14, and reading the slot width here would make the model's quantization noise
four times finer than the hardware's. That is the defect the word type exists to fix; it was latent
while a single `nbits` meant both numbers, and on the 4x2 preset it is now fixed rather than merely
expressible.

Between the quantizer and the bus sits one more step, and it is the only bit manipulation the
converter does: the stored value is **justified** into its container slot. Word↔slot layout still
goes through the [generated array serializers](../../vectorization/); the shift is the rule they
cannot know, and the word type owns it.

The other half of the contract, [slot order](./word.md#slot-order), is a property of the word
rather than of the converter, and lives on the sample-word page.

Both are checked against their C++ twins by
[a conformance test](../../../../tests/build/test_xsi_rfdc_samp.py) rather than against a re-reading
of the spec, and the end-to-end consequence is checked where it matters: a block that goes Python →
quantize → pack → real RTL → unpack → dequantize → Python comes back **bit-identical**.

## Next

- [Connecting the RF side](./rf_side.md) — the interface, the sources and sinks, and `t0`.
- [Connecting the fabric side](./axis_side.md) — packing, the word type, and the rate check.

**Source of truth:** `examples/rf_loopback/rfdc.py`, `tests/examples/test_rf_loopback.py`.
