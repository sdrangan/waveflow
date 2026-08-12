---
title: Python model
parent: RF loopback
nav_order: 1
audience: python
api: [Rfdc, RfSampPassThrough, RfLoopbackTB, RfLoopbackSim, RfDataSource, RfDataSink, RFSampIF]
summary: "Building the loopback in Python: the converter and its parameter split, the pass-through DUT, the testbench graph and the run procedure, and the stage-1 gate — a byte-identical loopback with both loss counters at zero. Then the two deliberate faults (a late producer, a stalled consumer) that drive those counters off zero by predicted amounts, because a counter that has never counted is not evidence."
---

# Python model

Everything on this page runs with no toolchain:

```bash
python -m examples.rf_loopback.rf_loopback
pytest tests/examples/test_rf_loopback.py tests/hw/test_rf_sample_if.py
```

## The converter

`Rfdc` is **one module carrying both directions**, not separate ADC and DAC blocks. The reason is
synchronization: the TX and RX sample counters have to hold a fixed relation, and that is a property
*of the converter*, not of two unrelated blocks — it is what lets the two grids' time origins have a
single owner.

```python
rfdc = Rfdc(name="rfdc", sim=sim, nbits=16, samp_per_word=4, full_scale=1.0,
            t0_rx=0.0, t0_tx=blk_period)
```

Four endpoints, in two pairs:

| endpoint | type | direction |
|---|---|---|
| `rx_rf` | `RFSampIFRx` | ADC path — RF blocks in from the environment |
| `rx_stream` | `StreamIFMaster` | ADC path — AXI-Stream words out to the fabric |
| `tx_stream` | `StreamIFSlave` | DAC path — AXI-Stream words in from the fabric |
| `tx_rf` | `RFSampIFTx` | DAC path — RF blocks out to the environment |

The two stream endpoints are the ones that would **cross the [cut](../../guide/flows/modules.md#the-cut)**
in an RTL build; the two RF endpoints stay behavioural on both sides of it.

With the metronome living in the [interface](../../guide/rf/sampling.md#the-metronome-lives-in-the-edge),
the converter is **reactive on the RF side**: it has no timer of its own and simply responds to block
arrivals.

### The parameter split

| parameter | binding | why |
|---|---|---|
| `n_rx`, `n_tx`, `nbits`, `iq_mode` | `HwParam` | they set the word layout synthesized logic is built *against* |
| `samp_per_word` | `HwParam`, **integer** | port width is `samp_per_word · nbits`; a sample cannot straddle a slot |
| `full_scale` | `DynParam` | an amplitude reference — one artifact serves every value |

`samp_rate` is deliberately **not** on this list. It lives on the RF interface's clock and the
converter *reads* it at bind; `t0` travels the other way and is *pushed*. Two declarations that can
disagree is the bug both directions exist to avoid.

There is also no `spc`. `samp_per_word` is the structural integer; everything else at this boundary
is a rate ratio — derived, and generally fractional. The Python model needs neither conversion,
because it works in seconds.

> **A trap on the `DynParam` row.** `discover_dyn_params` skips **falsy** values, so a `full_scale` of
> `0.0` would emit nothing and silently take a generated model's default. Zero is meaningless for an
> amplitude reference anyway, so the constructor refuses it — but the general shape of that trap
> applies to any numeric `DynParam`.

### Bit-exact quantization

"Evaluate the effect of bit widths in Python" only means anything if the Python does what the
hardware will. So quantization is the integer-backed
[`FixedField`](../../guide/schema/) — `ap_fixed<nbits, 1>` over `[-1, 1)`, rounding and
**saturating**, because a converter clips rather than wraps — and sample↔word packing goes through
the [generated array serializers](../../guide/vectorization/), never a hand-rolled `.range()`:

```python
quantized = from_real(samples / full_scale, self.SampType)   # ADC: real -> stored ints
yield from self.rx_stream.write(quantized)                   # packed at the stream's own width
```

At `nbits=16, samp_per_word=4` that is four samples per 64-bit AXI-Stream beat. The gate runs
`(8, 8)`, `(16, 4)`, `(12, 4)` and `(16, 2)` — including a non-power-of-two width — because the bugs
hand-rolled packing produces hide at exactly the awkward widths.

## The digital logic

`RfSampPassThrough` is a [`FreeRunMod`](../../guide/flows/concurrent.md): one burst in, the same
burst out.

```python
def run_iter(self) -> ProcessGen[None]:
    words = yield from self.s_in.get(nwords_max=int(self.nwords_blk))
    self.n_blk += 1
    yield from self.s_out.write(words)
```

Free-running is the honest kind here — logic sitting between two converter ports has no host to
start it and re-fires on each arriving block. The body is trivial so that the loopback golden is
*exact* rather than approximate: any difference between what went in and what came out is the
plumbing, not an algorithm.

## Graph and procedure

The same split as [`mem_copy`](../memcpy/python.md): `RfLoopbackTB.__post_init__` builds **only
structure**, because a component graph is data and can be walked, while a run-and-check function is
code and cannot. `RfLoopbackSim` owns the scenario, the run and the golden.

```python
adc_if = RFSampIF(name="adc_if", sim=sim, samp_clk=self.samp_clk,
                  n_ch=1, blksize=256, n_blk=8)
adc_if.bind("tx", self.source.rf_ep)
adc_if.bind("rx", self.rfdc.rx_rf)
```

`write_scenario` is the single scenario writer, so nothing can start from different bytes. It draws
samples **exactly on the converter's quantization grid** — `m / 2^(nbits-1) · full_scale` for integer
`m` — so a clean loopback is *bit*-identical rather than close. A tolerance would hide a packing bug,
and packing is what this example tests.

### The DAC tile has to start later than the ADC tile

The one number a loopback cannot leave at zero. The DAC grid is a metronome, not a queue: it emits a
block every period whether or not the samples for it have finished their trip through the fabric.
With both tiles started at the same instant, the first DAC period comes due before the first samples
arrive and one zero block goes out — `dac_if.underrun == 1`, with the ADC side unaffected.

That is the converter behaving *correctly*; the design is what is wrong, and `t0` is where a design
says so. The example lags the DAC epoch by one block period, comfortably more than the two AXI-Stream
bursts this pipeline costs.

## The gate

```python
sim = RfLoopbackSim(n_src_blk=8)
sim.run()
sim.check()
```

`check()` makes four claims, and the first two are the stage gate:

```
adc  {'blocks_sent': 8, 'blocks_delivered': 8, 'underrun': 0, 'overrun': 0}
dac  {'blocks_sent': 8, 'blocks_delivered': 8, 'underrun': 0, 'overrun': 0}
```

1. **Byte-identical.** The sink's bundle is compared to the source's *as bytes on disk*, not as
   arrays in memory — both participants are bundle-backed, so the loopback is a file-to-file
   comparison.
2. **`underrun == 0 and overrun == 0`** on both RF edges.
3. Block counts agree end to end (the DUT relayed as many bursts as the source sent).
4. **Alignment is derived**: DAC sample *n* occurs exactly one block period after ADC sample *n*, for
   every *n* — arithmetic on `t0` and the rate, not something a particular scheduling order made
   true.

## Why claim 2 is not redundant

Because claim 1 passes without it. Both failures below are **silent**: a starved grid emits
well-formed zero blocks and a stalled consumer simply sees fewer of them, and every functional check
downstream still passes on the data that did arrive.

So the counters are driven off zero deliberately, against *predicted* values — a counter that has
never counted is not evidence that it works.

### A late producer underruns

The source starts 2.5 block periods late, so periods 1 and 2 have nothing to send:

```python
sim.tb.source.start_delay = 2.5 * sim.tb.blk_period
```

`adc_if.underrun == 2` — exactly the two missed periods. And the padding is visible at the far end of
the loopback: the sink's first two blocks are all zeros, and real data resumes at the third with the
source's *first* block. Nothing was reordered or lost; the grid simply ran while the producer was not
ready.

### A stalled consumer overruns

The sink takes one block and then stops consuming forever. Its queue holds `depth` more, and
everything after that is dropped:

```python
RfLoopbackSim(n_src_blk=8, sink_stall_after=1, sink_depth=2)
```

`dac_if.overrun == 5` — that is `8 − 1 − 2`: eight blocks emitted, one consumed, two queued. At
`sink_depth=4` it is `8 − 1 − 4 = 3`, so the count is a model of the buffer rather than a constant
that happened to match once. `blocks_sent == blocks_delivered + overrun` holds throughout, and the
grid indices the sink *did* receive show the gap.

## What `check` says about these modules

An RF source, an RF sink and the converter declare neither realization hook at this stage, which is a
**finding**, not a declaration:

```pycon
>>> check(RfDataSource, "xsi_bfm_model")
(False, 'RfDataSource declares no bfm_model() hook, so it has no pre-written cycle model to place
 beside a top. A module realized OUTSIDE the cut overrides bfm_model() to name one; a module
 realized INSIDE the cut declares kernel_task() instead.')
>>> potential_targets(RfSampPassThrough)
frozenset({'composite_kernel'})
```

The contrast is what makes it meaningful: the digital logic *does* claim a target, because it is the
one module here that is meant to become hardware.

**Source of truth:** `examples/rf_loopback/`, `tests/examples/test_rf_loopback.py`.
