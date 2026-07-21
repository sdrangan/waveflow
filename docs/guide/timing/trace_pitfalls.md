---
title: Trace pitfalls
parent: Timing Analysis Tools
nav_order: 8
has_children: false
---

# Trace pitfalls — three ways a measurement goes silently wrong

A trace measurement rarely fails loudly. It returns a number, the number looks plausible, and it is
wrong. Each pitfall below produced a confident, incorrect conclusion before it was caught — and each
is baked into the Waveflow trace tooling so you inherit the fix. They are worth understanding anyway,
because the moment you step outside the provided accessors they return.

## 1. Sample mid clock-low, not on the rising edge

A VCD records a signal change caused by a clock edge **at that edge's timestamp**. So if you sample a
synchronous signal *at* the rising edge, you read the value the edge just produced — the *post*-edge
value — not the value the flops captured.

For a single registered signal that is only a one-cycle shift. For a **two-wire handshake**
(`VALID & READY`) it is worse: the two wires can move at the same timestamp in opposite directions,
so sampling on the edge both **invents** coincidences (both high post-edge that were not both high
during the cycle) and **destroys** them (a handshake that completed, erased because one wire dropped
on the edge). On a real `mem_copy` trace this read AXI-MM `AW` as **16** accepted addresses instead
of **128** — an 8× undercount — and made the writer look like the faster stage when it is the
bottleneck.

The fix is to sample **a quarter period before** each rising edge — the middle of the clock-low
phase, where every synchronous signal is stable at the value the next edge will capture. Half a
period lands on the *falling* edge, another transition boundary, and degrades again.

```python
from waveflow.utils.vcd import extract_clock_times, clock_sample_times, resample_signal

edges = extract_clock_times(clk_sig)          # the rising edges
grid = clock_sample_times(edges)              # a quarter period earlier
value = resample_signal(sig, grid)
```

`extract_axis_bursts`, `extract_aximm_bursts`, `extract_fifo_bursts` and every `BoundTrace` accessor
already do this. Beats are still **labelled** by the true edge time, so `tstart` and cycle indices
are unchanged — only the *reading* moves.

## 2. A firing ends at `ap_done`, not the last output beat

It is tempting to time a component from its first input beat to its last output beat. That is wrong
whenever the component issues `m_axi` **writes**, because an `m_axi` store is **posted**: it retires
when the adapter *accepts* the word, not when the beat reaches the bus. The component's own code has
moved on; the write burst is still draining behind it.

On `mem_copy`'s writer, three plausible window-ends give three different stories:

| window ends at | firing span | conclusion it supports |
| --- | --- | --- |
| `s_done` (last stream output) | 155 | "the reader is the bottleneck; the writer idles" |
| last `B` response | 180 | "a few cycles of restart latency" |
| **`ap_done`** | **183** | **the writer is 100% utilised — the bottleneck** |

Only the last is right. `ap_done` is unambiguous because **HLS holds it until the firing's
outstanding writes have responded** — it is the one signal that means "this firing is actually done".

The subtlety: a free-running top is `ap_ctrl_none` and has **no** control interface. But each
`hls::task` instance *inside* it is an ordinary `ap_ctrl_hs` block with `ap_start`/`ap_continue`
tied to `1'b1` (you can see the `assign …_ap_start = 1'b1;` in the generated top), so `ap_done` still
pulses once per firing — and Vitis lifts the pin into the top scope where a level-1 dump sees it.

`BoundTrace.component_firings()` anchors on it; `Firing.span` is first-input → `ap_done`.

{: .note }
> A consequence worth holding separately from timing: because the store is posted, a response emitted
> *inside* the firing (like `mem_copy`'s `CopyResp`) can go out **before** the data it reports has
> actually landed in memory. If a downstream consumer must not read the destination until the write
> is durable, the guarantee has to ride the **task boundary** (`ap_done`), not a stream write inside
> the body.

## 3. Read backpressure from occupancy, not the write enable

To measure whether a producer is being throttled, the obvious signal is its write handshake —
"cycles where it wanted to write but the channel was full". **That signal does not exist.** HLS
*gates* the write enable: a task blocked on a full channel stalls its whole pipeline **without ever
asserting `write`**. So a `write & !full_n` metric reports **zero** backpressure precisely when the
producer is most stuck.

The reliable signal is the FIFO's own **occupancy counter**. A channel at capacity is a producer
that cannot push:

```python
blocked = bt.channel_blocked("copy_data", firing.start, firing.end)
```

which compares `<ch>_num_data_valid` against `<ch>_fifo_cap` (both named in the manifest, both dumped
at level 1). This is what located `mem_copy`'s 30 cycles/job of blocking — invisible on every write
handshake in the design.

## Why these matter for calibration

All three share a theme: **a component's true occupancy is not what its stream ports say.** It keeps
working after its last output (2), it stalls without signalling it (3), and even reading the ports
correctly requires the right sampling phase (1). A timing model calibrated on the wrong window folds
contention and posted-write drain into the component's own cost — and then composition stops
predicting anything, because the contention was supposed to *emerge* from the interconnect, not be
baked into the block. Getting the window right is what lets you calibrate on uncontended firings and
have congestion fall out on its own. The [memcpy timing](../../examples/memcpy/timing.md) page walks
that through end to end.
