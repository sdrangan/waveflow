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

and applies it to every symbol of the row: $z_{i,j} = g_i \, r_{i,j}$.

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

## What you will be able to do

1. Define the protocol of a stream that carries several kinds of data per command, and a status stream.
2. Split a computation into stages by rate, and give each stage its own copy of one input with a
   [**`StreamBroadcast`**](../../guide/streams/broadcast.md), each stage consuming its whole copy.
3. Write a [**composite**](../../guide/comp_codegen/freerunning_composite.md) of free-running modules: children,
   internal framed streams, a boundary.
4. Carry a command and its errors from one stage to the next, so the last stage answers.
5. Write each stage's **HLS body** once, with its port types as template parameters, so the same body runs
   as a stage alone and inside the composite.
6. Verify each stage alone in its own [**harness**](../../guide/flows/units.md), then the composite, in pysim and
   at RTL, with float32 results compared by their bits.

## The pages

- [The protocol](protocol.md) -- one input, three bursts a command; the two outputs; fatal errors.
- [The stages](stages.md) -- `gain` and `apply`, in Python and in HLS; the composite.
- [The system flow](system_flow.md) -- each stage alone, then the composite; the gates.

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
