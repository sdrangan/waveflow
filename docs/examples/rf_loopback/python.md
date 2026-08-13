---
title: Python model
parent: RF loopback
nav_order: 1
audience: python
api: [Rfdc, RfSampPassThrough, RfLoopbackTB, RfLoopbackSim, RfDataSource, RfDataSink, RFSampIF]
summary: "Building the loopback in Python: the converter and its parameter split, the pass-through DUT, the testbench graph and the run procedure, and the stage-1 gate — a loopback that is byte-identical once shifted by the pipeline's declared block latency, with loss exactly as declared. Then the two deliberate faults (a late producer, a stalled consumer) that drive those counters off zero by predicted amounts, because a counter that has never counted is not evidence."
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

### A loop through the RF grids costs one block, structurally

The DAC grid is a metronome, not a queue: it emits a block every period whether or not the samples
for it have arrived. And DAC block *k* **cannot** carry ADC block *k* — the ADC only delivers block
*k* at the instant the DAC period for it comes due, so the loop costs at least one block *index*
**however fast the fabric is**. A zero-latency fabric would not close it either. That is
`plans/adc_model.md`'s "no dependency within < `blksize` samples", applied to the fabric path.

So the pipeline declares what it costs:

```python
class RfSampPassThrough(FreeRunMod):
    blk_latency: HwParam[int] = 1      # >= 1 for any block-processing module
```

and `blk_latency = 0` is **refused at construction** rather than reported later as a symptom — a loop
that claims to be free is not a slow system, it is not a system.

Two things this deliberately does *not* do. It does not stagger the tile epochs: `t0_rx == t0_tx` is
what MTS gives you, and buying pipeline latency by starting a tile late would model the thing MTS
exists to prevent. And it does not treat the resulting first-block underrun as a fault — a converter
fed by a pipeline **must** underrun until the data reaches it, which is exactly why a real design
primes its buffer before enabling the tile.

The declaration is *checked*, not trusted: the gate asserts the DAC edge underran exactly
`blk_latency` times **and** that the last one was inside the transient, so a module that claims two
blocks and exhibits one fails just as loudly as one that drifts in steady state.

## The gate

```python
sim = RfLoopbackSim(n_src_blk=8)
sim.run()
sim.check()
```

`check()` makes four claims, and the first two are the stage gate:

```
adc  {'blocks_sent': 8, 'blocks_delivered': 8, 'underrun': 0, 'overrun': 0}
dac  {'blocks_sent': 8, 'blocks_delivered': 8, 'underrun': 1, 'overrun': 0}
```

1. **Byte-identical, once shifted by the declared latency.** The sink's bundle is compared to the
   source's *as bytes on disk*, not as arrays in memory — both participants are bundle-backed, so
   the loopback is a file-to-file comparison. DAC block *k* must equal ADC block *k* − `blk_latency`,
   and the leading `blk_latency` blocks must be exactly the zero-fill.
2. **Loss is exactly what the graph declared.** `underrun == 0` on the ADC edge, which is fed
   straight from the source and entitled to nothing; `underrun == blk_latency` on the DAC edge, and
   at the *start* — `assert_clean(startup_blocks=…)` checks the grid index too, so a steady-state
   fault cannot hide inside a transient's budget. `overrun == 0` everywhere: overrun has no
   transient to hide in.
3. Block counts agree end to end (the DUT relayed as many bursts as the source sent).
4. **Alignment is derived**: with both tiles on one epoch, DAC sample *n* occurs at the same instant
   as ADC sample *n*, for every *n* — arithmetic on `t0` and the rate, not something a particular
   scheduling order made true. Note what this is *not* claiming: aligned grids do not make the loop
   free. Alignment is about when a grid ticks; `blk_latency` is about which block each tick carries.

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
the loopback, shifted by the pipeline's declared block: the sink's first three blocks are all zeros
(the DAC's own structural startup block, then the two the ADC zero-filled) and real data resumes at
the fourth with the source's *first* block. Nothing was reordered or lost; the grid simply ran while
the producer was not ready.

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

## The digital logic becomes hardware {#synthesis}

`RfSampPassThrough` is the one module in this graph that is *meant* to become RTL, and it now does.
It is verified **cut alone** — `StreamDriver → dut → StreamSink`, generic AXI-Stream BFMs, no
converter and no RF edges — by `examples/rf_loopback/rf_dut_build.py`.

That is not a second design. It is the *same module under a different cut*, which is the property
[the cut is a build choice](../../guide/flows/modules.md#the-cut) asserts; this is the first place it
is exercised rather than stated. It also keeps two risks apart: whether the DUT synthesizes and runs
at RTL is one question, whether the converter models drive it correctly is another, and answering the
first with generic BFMs means a later failure has one place to be.

### The body is generated

Not hand-written. `run_iter` extracts to:

```cpp
static void rf_pass_through_task(hls::stream<ap_uint<64> >& s_in,
                                 hls::stream<ap_uint<64> >& s_out) {
    UInt64Array blk;
    blk.read_stream<64>(s_in);
    blk.write_stream<64>(s_out);
}
```

Getting there needed two changes to the Python, and both are worth knowing because they are rules,
not quirks:

- **A pysim counter cannot be read inline in a synthesizable body.** `self.n_blk += 1` trips the
  implicit-capture rule, which cannot tell a baked-in constant from a register someone must write
  from a counter with no hardware meaning. `@sim_only` is the answer for the third — and it has to
  sit on a **method**, because the check is an attribute on the resolved object and an `int` cannot
  carry one. (`add_state` would be wrong: it declares persistent *hardware* storage.)
- **Use the typed `get`.** `get(nwords_max=N)` is the raw-word convention for non-`HwModule` callers;
  it carries no schema type and the extractor has no rule for it. The payload type here comes from a
  `blk_words` property that specializes a `DataArray` from the module's own `HwParam`s — one
  declaration serving every width the pysim tests sweep, and a concrete type at extract time.

### What each layer proves

| layer | says | does **not** say |
|---|---|---|
| `check(…, "composite_kernel")` | the graph lowers | anything about the body — for a leaf it never runs the extractor |
| pysim | the relay is bit-identical in Python | anything about RTL |
| csynth | the RTL exists **and has a datapath** | that it is correct — a DCE'd kernel still reports success |
| XSI | the real RTL relays the words | — |

The csynth check therefore asserts the *module set*, not the exit code: the task module, the two
pipelined loops (the read and the write), and the block RAM between them. A top with nothing under it
is exactly what a silently optimized-away kernel looks like.

### The gate

8 bursts × 64 words = 512 words, relayed bit-identically, with the last word landing at cycle
**1072**.

That number is the DUT's honest cost. The generated body is `read_stream` then `write_stream` — two
sequential pipelined loops over one block RAM — so a firing does **not** overlap its read and write,
and each block costs about 143 cycles rather than 128. Overlapping them would need two tasks and a
channel between them, which is what `mem_copy` does and why its per-job cost is roughly a `max()`
rather than a sum. Recorded in `tests/examples/test_xsi_bfm.py` beside the other cycle gates.

**Source of truth:** `examples/rf_loopback/`, `tests/examples/test_rf_loopback.py`,
`tests/examples/test_rf_dut_synth.py`.
