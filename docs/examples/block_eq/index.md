---
title: Block equalizer
parent: Examples
nav_order: 0.7
has_children: true
example_dir: examples/block_eq
summary: "The second example: a stream-only pipeline of three free-running modules. One input stream carries, per command, the channel estimates and the received symbols; the framework's StreamBroadcast gives a copy to each of two stages -- gain computes the MMSE equalizer g = conj(h)/(|h|^2+N) once a row (a division), apply multiplies every symbol by its row's g -- and gain passes g to apply on an internal stream. Each stage keeps consuming its own copy (the broadcast's rule), errors are fatal (a zero noise, a burst that ends early), and the arithmetic is float32 in a fixed operation order, bit-exact with signed zeros. Each stage is verified alone in its own harness, then the composite, in pysim and at RTL -- one hand-written body serving a stage in both places."
---

# Block equalizer

A receiver's equalizer as a **pipeline of free-running modules on streams**. Symbols arrive in rows, each
row through its own channel `h[i]` with noise of variance `N[i]`; the kernel computes, per row, the MMSE
equalizer

$$
g_i = \frac{\overline{h_i}}{|h_i|^2 + N_i}
$$

and applies it to every symbol of the row: $z_{i,j} = g_i \, r_{i,j}$. If those terms are new, start
with [What an equalizer does](theory.md).

It is the second example because it adds exactly one thing to the [free-running polynomial](../stream_kernel/index.md):
**several modules**. A division once a row and a multiply once a symbol run at different rates, so they
are two stages; one input carries what both need, so the framework's
[`StreamBroadcast`](../../guide/streams/broadcast.md) gives each its own copy; and `gain` hands its result to
`apply` on a stream between them.

```text
                     +----------------------- block_eq -----------------------+
source --s_in--> [ StreamBroadcast(2) ] --> [ gain ] --g--> [ apply ] --m_out-----> sink
                                       ----------------->  [ apply ] --m_status--> sink
```

## Learning Objectives

Only what this example adds to the [free-running polynomial](../stream_kernel/index.md#learning-objectives),
in the order the pages teach it:

1. Compute a row's **MMSE equalizer gain** `g = conj(h) / (|h|^2 + N)` by hand, and say what it does that
   `1/h` does not on a weak channel.
2. Define a protocol whose one input carries **several kinds of data per command** (the command, the
   estimates, the symbols: three bursts), and a message **between stages** that carries the command and
   its errors to the last stage.
3. Write a **float32 golden in a fixed operation order** -- one rounding per operation, no fused
   multiply-add -- and compare results by their bits, signed zeros included.
4. **Split a computation into stages where its rate changes**: a division once a row, a multiply once a
   symbol.
5. Give two stages one input with a [**`StreamBroadcast`**](../../guide/streams/broadcast.md), and order
   each stage's reads by the **keep-consuming rule**, so the broadcast's lockstep cannot deadlock.
6. Write a [**composite**](../../guide/comp_codegen/freerunning_composite.md) of free-running modules:
   children, internal framed streams with depths sized against what can pile up in them, a boundary.
7. Write each stage's **HLS body once**, with its port types as template parameters, so the same body runs
   as a stage alone and inside the composite.
8. Verify each stage alone in its own [**harness**](../../guide/flows/units.md) -- the downstream stage
   driven by the golden's link messages -- then the composite: four systems on one build DAG, in pysim and
   at RTL.

## The pages

- [What an equalizer does](theory.md) -- the channel, the equalizer, the MMSE gain, in plain words and
  one worked number.
- [The protocol](protocol.md) -- one input, three bursts a command; the two outputs; fatal errors.
- [The golden](golden.md) -- the schemas, the float32 operation order, the worked examples, the
  scenarios.
- [The stages](stages.md) -- why two, the broadcast and its rule, `gain` and `apply` in Python, the
  composite and its link depths.
- [The HLS bodies](hls.md) -- each stage's body loop by loop; one body, two placements.
- [The system flow](system_flow.md) -- four systems on one DAG; the gates.

## Running it

```bash
python -m examples.block_eq.block_eq_build --through pysim          # pysim only
python -m examples.block_eq.block_eq_build                          # the composite through compare
python -m examples.block_eq.block_eq_build --through gain_compare   # gain alone, at RTL
python -m examples.block_eq.block_eq_build --through apply_compare  # apply alone, at RTL
python -m examples.block_eq.block_eq_build --through early_compare  # the fatal early TLAST, at RTL
```

The gates are [`tests/examples/test_block_eq.py`](../../../tests/examples/test_block_eq.py). Measured with
Vitis HLS and Vivado 2025.1.
