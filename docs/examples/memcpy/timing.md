---
title: Timing instrumentation
parent: Memory Copy
nav_order: 7
---

# Timing — where the 183 cycles go

The [RTL simulation](./rtlsim.md) page ends with a number: **2908 cycles** for 16 jobs, a
steady-state period of **183 cycles/job**, where the [pysim model](./testbench.md) said 140. This
page closes that gap — not by fitting a fudge factor, but by *attributing* every cycle to a named
signal.

To do that we need to see inside the kernel: the two internal `framed_word` FIFOs (`cmd` and
`copy_data`) that wire the three tasks, and both `m_axi` bundles. Getting those signals out of an
RTL run, by exact name, is general Waveflow machinery — the four
[trace steps](../../guide/timing/trace_steps.md) — so this page uses it rather than re-explaining it:

```bash
python examples/mem_copy/mem_copy_build.py --through extract_bursts
```

That produces `mem_copy_trace.vcd` (the waveform), `mem_copy_trace.json` (the net-name manifest), and
`mem_copy_timing.json` (a per-firing timing table). A fifth step, `timing_figures`, renders the
pictures below; `sync_docs_figures` promotes them into the docs.

## The run at a glance

![Stage activity across the whole run](./images/timeline_full.svg)

Every stage of the pipeline on one cycle axis, 16 jobs. Three things are visible before any analysis:
the **183-cycle cadence**; that `gmem0 R` (reads) and `gmem1 W` (writes) are busy *simultaneously*
rather than in turn, so the design really is pipelined; and that the writer's green band is nearly
continuous while the command lanes at the top are almost entirely idle. The writer is doing something
close to all the work.

## The per-firing table

Recall that free-running components (`FreeRunComp`) repeatedly call a `run_iter` function. Each
iteration begins when its input data is available, then terminates and waits for the next. The
[`ExtractBurstsStep`](../../guide/timing/trace_steps.md) writes one row per such **firing** of each
component:

```json
{"component": "mem_w_stream_framed_done_task", "index": 8,
 "start": 1488, "end": 1670, "span": 183,
 "nwords": 128, "num_trans": 8, "blocked": 0}
```

`span` is the firing's true occupancy — first input handshake to `ap_done`, *not* its last output
beat, because the writer's `m_axi` stores are posted and keep draining after `s_done`
([why that matters](../../guide/timing/trace_pitfalls.md#2-a-firing-ends-at-ap_done-not-the-last-output-beat)).
`blocked` is cycles its output channel sat at capacity, read from the FIFO occupancy counters rather
than a write enable ([why](../../guide/timing/trace_pitfalls.md#3-read-backpressure-from-occupancy-not-the-write-enable)).

Two facts fall straight out of it.

**`blocked == 0` isolates the rows you may calibrate on.** Only the reader's *first* firing is
uncontended:

| component | firing | span | blocked |
| --- | --- | --- | --- |
| `mem_r_stream` | 0 | **153** | 0 |
| `mem_r_stream` | 1–15 | 183 | **30** |
| `mem_w_stream` | 0–15 | **183** | 0 |

![Per-firing span, split into own work and waiting](./images/firing_spans.svg)

Colour is the component's own work, grey is waiting on a full channel — and the bottleneck needs no
arithmetic to spot. The writer is solid green at 183 on every firing: busy the entire period. The
reader is 153 on firing 0, then 153 + 30 grey. And the sequencer is ~5 cycles of work against ~175 of
waiting — it finishes a command almost immediately and then sits behind everything downstream.

So the writer is the bottleneck, 100% utilised at 183 of a 183-cycle period. The reader's 30 cycles
are it waiting on a writer that is still draining — *emergent* congestion, not a component property,
and exactly what must **not** be baked into a component's model.

## Where the 30 cycles actually are

![One job, beat by beat, with FIFO occupancy](./images/timeline_job.svg)

One steady-state firing. The lower panel is the `copy_data` FIFO's occupancy, and the shaded band is
it at **capacity** — the reader blocked, unable to hand over its descriptor words, because the writer
has not finished draining the *previous* job. The reader's `gmem0 AR` bursts (top panel) only begin
after that band clears.

Look also at the far right: `s_done out` fires, and `gmem1 W` **keeps going for another ~24 cycles**.
That is the posted-write drain — the reason a firing must end at `ap_done` and not at `s_done`.

## The law

Sweeping `n_words` from 32 to 512 (a 16× range), the writer's firing span is exactly:

```
span = 41 + n + 2 × (ceil(n/16) − 1)
```

One cycle per word, **two cycles per AXI burst boundary** (HLS's `max_burst_length` is 16), plus a
fixed control cost — the two-cycle term read straight off the gaps between W bursts in the trace. The
same structure appears as a two-level split in the table itself: subtract the bus occupancy
`nwords + 2 × (num_trans − 1)` and a **constant** remains per component:

| component | `span − bus` |
| --- | --- |
| `mem_w_stream` | **41** |
| `mem_r_stream` | **11** |

Same bus law, different per-component constants. The burst term is a *platform* property of the
`m_axi` adapter — it belongs on a [`BusTiming`](../../guide/timing/aximm.md) model shared by every
accelerator; the constant is the component's own control cost.

## Why this is a model, not a fit

The two constants were measured on **uncontended** firings only (`blocked == 0`). Feed them back into
the SimPy model, give the internal channels their real depth of 2, and the 30 cycles of congestion
**emerge** rather than being fitted — reproducing the RTL across the whole 16× range to **1.1%**,
versus 27% for a single-point fit. That is the payoff of getting the measurement window right: a
component model calibrated in isolation that still predicts the contended system.

Implementing that split in the framework — `BusTiming` on the memory slave, the fixed latency on the
component, channel depth from the same declaration codegen uses — and automating the fit, is the next
arc. See [`plans/memcpy_timing_calibration.md`](https://github.com/sdrangan/waveflow/tree/main/plans/memcpy_timing_calibration.md).

## See also

- [Tracing a kernel run](../../guide/timing/trace_steps.md) — the four steps, in general
- [Trace pitfalls](../../guide/timing/trace_pitfalls.md) — the three traps this relied on avoiding
- [RTL simulation](./rtlsim.md) — the run this instruments
