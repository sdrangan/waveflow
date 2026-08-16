---
title: Taking it to RTL
parent: RF loopback
nav_order: 3
audience: hls
api: [RfSampPassThrough, kernel_task, bfm_model, check, potential_targets, sim_only]
summary: "Step 7: the digital logic becomes hardware. What check says about each module in the graph, the DUT synthesized cut alone between generic AXI-Stream BFMs, one task body generated and one handed over, what each verification layer actually proves, and the recorded XSI cycle gate."
---

# Taking it to RTL

Step 7, and the first page here that needs a toolchain:

```bash
pytest -m vitis tests/examples/test_rf_dut_synth.py     # Vitis HLS
pytest -m xsi  tests/examples/test_xsi_bfm.py           # Vivado xsim
```

## What `check` says about these modules

`check` answers **per module**, and the answer is a *finding* about that module rather than something
the module says about itself.

```pycon
>>> check(RfSampPassThrough, "xsi_bfm_model")
(False, 'RfSampPassThrough declares no bfm_model() hook, so it has no pre-written cycle model
 to place beside a top. A module realized OUTSIDE the cut overrides bfm_model() to name one; a
 module realized INSIDE the cut declares kernel_task() instead.')
>>> potential_targets(RfSampPassThrough)
frozenset({'composite_kernel'})
```

That is not a complaint. The DUT belongs **inside** the cut, so a model to place *beside* a top is
exactly what it should not have — and the second line says what it does have instead.

The RF environment gives the contrasting answer. `RfDataSource`, `RfDataSink` and `Rfdc` each name a
C++ model now, so they lower *outside* the cut; but none of them claims a codegen target:

```pycon
>>> potential_targets(RfDataSource)
frozenset()
```

A node that participates in the graph and is never synthesized into it is the third row of the kinds
table, and it is a legitimate row rather than an omission.

## The digital logic becomes hardware

`RfSampPassThrough` is the one module in this graph that is *meant* to become RTL, and it does. It is
verified **cut alone** — `StreamDriver → dut → StreamSink`, generic AXI-Stream BFMs, no converter and
no RF edges — by `examples/rf_loopback/rf_dut_build.py`.

That is not a second design. It is the *same module under a different cut*, which is the property
[the cut is a build choice](../../guide/flows/modules.md#the-cut) asserts; this is the first place it
is exercised rather than stated. It also keeps two risks apart: whether the DUT synthesizes and runs
at RTL is one question, whether the converter models drive it correctly is another, and answering the
first with generic BFMs means a later failure has one place to be.

### One body is generated, one is handed over

The top is derived from the graph — two tasks and the channel between them, exactly as `mem_copy`'s
is:

```cpp
hls_thread_local hls::stream<ap_uint<64> > blk_fifo;
#pragma HLS STREAM variable=blk_fifo depth=64
hls_thread_local hls::task t0(rf_samp_ingress_task<64>, s_in, blk_fifo);
hls_thread_local hls::task t1(rf_samp_relay_task, blk_fifo, s_out);
```

The block relay's body is **generated** from its `run_iter`:

```cpp
static void rf_samp_relay_task(hls::stream<ap_uint<64> >& blk_in,
                               hls::stream<ap_uint<64> >& s_out) {
    UInt64Array blk;
    blk.read_stream<64>(blk_in);
    blk.write_stream<64>(s_out);
}
```

The ingress's is **hand-written**, and the reason is a real boundary rather than a gap in the
extractor:

```cpp
template <int W>
static void rf_samp_ingress_task(hls::stream<ap_uint<W> >& s_in,
                                 hls::stream<ap_uint<W> >& w_out) {
    w_out.write(s_in.read());
}
```

One word in, one word out — that is what "never stops reading" means in hardware. **pysim cannot say
that.** `StreamIFSlave.get` pops one burst and truncates it to the width asked for, so a word-granular
Python body would silently discard 63 of every 64 words; a burst is pysim's quantum. So the module
[overrides `kernel_task()`](../../guide/comp_codegen/freerunning_override.md) and leaves `run_iter` as
the pysim twin, relaying a whole burst. The two are identical at block granularity, which is the only
granularity pysim resolves.

Getting the generated half there needed two changes to the Python, and both are worth knowing because
they are rules, not quirks:

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
| `check(…, "composite_kernel")` | the graph lowers | anything about the body — it never runs the extractor |
| pysim | the relay is bit-identical in Python | anything about RTL |
| csynth | the RTL exists **and has a datapath** | that it is correct — a DCE'd kernel still reports success |
| XSI | the real RTL relays the words | — |

The csynth check therefore asserts the *module set*, not the exit code: **both** task modules, at
least two pipelined loops (the read and the write), a block RAM between them, and the internal FIFO
as `rf_pass_through_fifo_w64_d64_A` — the depth-64 channel, present in the RTL because it is
*internal*. A top with nothing under it is exactly what a silently optimized-away kernel looks like —
and with two tasks there are now two things that could vanish.

## The gate

8 bursts × 64 words = 512 words, relayed bit-identically, with the last word landing at cycle
**1066**.

It was 1072 before the two-task split, and **the six cycles are the point**: the change was not made
to go faster. The block stage still runs two sequential pipelined loops over one block RAM, so its
per-block cost is essentially what it was, and this testbench — whose `StreamDriver` pushes at full
rate — is bound by that. The read/write serialization inside a block stage is intrinsic to block
processing, not a defect: a stage that transforms a block cannot emit before it has received one.

What changed is *where the stall lands*. Here it lands on a driver that waits, so it is invisible; in
the loopback it lands on a converter that cannot, which is where the change is measured — the ADC
went from dropping 72 words to dropping none. Both numbers live in
`tests/examples/test_xsi_bfm.py` and `tests/examples/test_rf_loopback_xsi.py`.

## See also

- [The fidelity boundary](../../guide/rf/python/fidelity.md) — why that 72-word loss was invisible in pysim,
  and still would be.
- [Overriding a free-running body](../../guide/comp_codegen/freerunning_override.md) — the
  `kernel_task()` hook.

**Source of truth:** `examples/rf_loopback/rf_dut_build.py`,
`tests/examples/test_rf_dut_synth.py`, `tests/examples/test_xsi_bfm.py`.
