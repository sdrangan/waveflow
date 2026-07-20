---
title: Timing instrumentation
parent: Memory Copy
nav_order: 7
---

# Timing instrumentation — measuring where the cycles go

The [RTL simulation](./rtlsim.md) page ends with a number: **2908 cycles** for 16 jobs, or a
steady-state period of **183 cycles/job**. The pysim model says 140. This page is about closing that
gap — not by fitting a fudge factor, but by *attributing* it to named signals.

Everything here is one build command:

```bash
python examples/mem_copy/mem_copy_build.py --through extract_bursts
```

That runs four steps beyond the [testbench codegen](./codegen_tb.md). Two of them cost nothing (they
are pure `elaborate()`), one needs the toolchain, and the last is waveform analysis. A fifth
(`timing_figures`) renders the pictures on this page; `sync_docs_figures` promotes them into
`docs/`.

![Stage activity across the whole run](./images/timeline_full.svg)

Every stage of the pipeline on one cycle axis, 16 jobs. Three things are visible before any
analysis: the **183-cycle cadence**, the fact that `gmem0 R` (reads) and `gmem1 W` (writes) are busy
*simultaneously* rather than in turn — the design really is pipelined — and that the writer's green
band is nearly continuous while the command lanes at the top are almost entirely idle.

## The problem: XSI cannot see inside the kernel

`xsi_get_port_number` resolves **top-level ports only**. The interesting signals in `mem_copy` are
not ports — they are the two internal `framed_word` FIFOs (`cmd` and `copy_data`) that wire the
three tasks together. Through XSI's value API they are unreachable.

The way in is `$dumpvars`, from a small Verilog module elaborated *alongside* the DUT.

## Step 1 — `AddVcdTopStep`: the dumper

Writes `xsi/vcd_dumper_<top>.v`:

```verilog
module vcd_dumper_mem_copy;
    initial begin
        $dumpfile("mem_copy_trace.vcd");
        $dumpvars(1, mem_copy);
    end
endmodule
```

Two details carry all the weight.

**It is a second top, not a wrapper.** `run.bat … trace` elaborates `work.mem_copy
work.vcd_dumper_mem_copy`. The DUT is untouched, so every BFM port number and every cycle count is
identical to an untraced run — the trace *observes*, it does not perturb.

**The level is 1, not 0.** Level 1 dumps this scope's own signals and does not descend into
children. That sounds like it would miss the internal FIFOs, and it does not: **Vitis lifts
inter-task dataflow channel wires up into the top scope**, beside the task instances. So a level-1
dump already reaches

```
cmd_dout / cmd_empty_n / cmd_full_n          the channel itself
mem_seq_..._U0_cmd_din / _cmd_write          the producer's side
mem_r_stream_..._U0_cmd_read                 the consumer's side
<task>_U0_ap_done                            each task's firing boundary
```

with **no hierarchical path to resolve**. Level 0 would dump the entire subtree for no extra reach.
The cost stays bounded: 258 signals for `mem_copy`, 363 for `interleaver_canon`.

The module is named after the top because one `xsi/` directory can serve several tops
(`examples/interleaver/xsi` builds three), and a dumper naming a scope that is not part of *this*
elaboration is a hard error.

## Step 2 — `TraceManifestStep`: what the nets are called

Writes `results/mem_copy_trace.json`, mapping the Python graph onto RTL net names. Derived entirely
from `elaborate()` — no RTL is read, nothing is simulated.

Why a manifest instead of matching names in the waveform? Because substring matching is not merely
fragile here, it is **wrong**. An interleaver trace contains both

```
ywords_fifo_cap[2:0]                       the channel's counter
il_store_..._U0_ywords_fifo_cap[31:0]      the instance's own copy
```

Different widths, different meanings, and a matcher takes whichever it sees first. The names are
already known — codegen chose them — so the manifest binds *exactly*, and a net that has gone
missing fails loudly instead of silently extracting nothing.

Only the `_U0` instance suffix is Vitis's choice; channel names are the C++ stream variables, port
names are the boundary names, `m_axi` nets are named after the bundle.

## Step 3 — `RtlSimStep`: run it, tracing

A thin wrapper over `xsi/run.bat`, passing its third argument `trace`.

**This step asserts nothing.** It runs and produces `xsi/mem_copy_trace.vcd`. The exact cycle count
stays with the `-m xsi` gate, which calls `run.bat` directly — routing a green gate through new code
is how a gate quietly stops meaning what it meant.

{: .warning }
> **Re-running the built `.exe` does not regenerate the VCD.** Only the full `run.bat` path does,
> because the dump comes from the elaborated snapshot. A sweep that re-ran the binary per data point
> once silently re-measured the *previous* trace and reported an identical period at five different
> job sizes. `RtlSimStep` deletes the VCD first and fails if it does not reappear.

## Step 4 — `ExtractBurstsStep`: the timing table

Binds the manifest to the waveform and writes `results/mem_copy_timing.json` — one row per
**firing** of each component:

```json
{"component": "mem_w_stream_framed_done_task", "index": 8,
 "start": 1488, "end": 1670, "span": 183,
 "nwords": 128, "num_trans": 8, "blocked": 0}
```

- `span` — first input handshake → `ap_done`
- `nwords` / `num_trans` — `m_axi` beats and bursts inside the firing, *measured* off the trace
- `blocked` — cycles this component's output channel sat at capacity

That is deliberately the shape a calibration consumes: `span` regressed over `{nwords, num_trans}`.
It is also everything the figures need.

## Reading the result

Two facts fall straight out of the table.

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
reader is 153 on firing 0, then 153 + 30 grey. And the sequencer is ~5 cycles of work against ~175
of waiting: it finishes a command almost immediately and then sits behind everything downstream.

The writer is never blocked — it is the bottleneck, 100% utilised at 183 of a 183-cycle period. The
reader's 30 cycles are it waiting on a writer that is still draining. That is *emergent* congestion,
not a component property, and it is exactly what must **not** be baked into a component's model.

### Where the 30 cycles actually are

![One job, beat by beat, with FIFO occupancy](./images/timeline_job.svg)

One steady-state firing. The lower panel is the `copy_data` FIFO's occupancy, and the shaded band is
it sitting **at capacity** — the reader blocked, unable to hand over its descriptor words, because
the writer has not finished draining the *previous* job yet. Reading right to left across the top
panel: the reader's `gmem0 AR` bursts only begin after that band clears.

Look also at the far right of the top panel. `s_done out` fires, and `gmem1 W` **keeps going for
another ~24 cycles afterwards**. That is the posted-write behaviour that makes `ap_done` the only
honest place to end a firing.

**Subtract the bus occupancy and a constant remains.** With `bus = nwords + 2 × (num_trans − 1)`:

| component | `span − bus` |
| --- | --- |
| `mem_w_stream` | **41** |
| `mem_r_stream` | **11** |

Same bus law, different per-component constants — which is the two-level calibration split made
visible: the burst term is a *platform* property of the `m_axi` adapter, the constant is the
component's own control cost.

## Three traps this instrumentation exists to avoid

**Sample mid clock-low, not on the rising edge.** A VCD records a change caused by an edge *at* that
edge's timestamp, so sampling there reads the value the edge produced. For a two-wire handshake that
is not a clean one-cycle shift — it both invents and destroys coincidences. On this trace it read
AXI-MM `AW` as 16 accepted addresses instead of 128. `waveflow.utils.vcd.clock_sample_times` steps
back a quarter period; beats are still *labelled* by the true edge time.

**A firing ends at `ap_done`, not at the last output beat.** An `m_axi` store is *posted* — it
retires when the adapter accepts the word, not when the beat is on the bus. `mem_copy`'s writer
emits `s_done` at cycle 1642, its last W beat lands at 1666 and its B response at 1667. Anchoring on
the output beat measures 155 where the firing is 183, and *inverts which stage looks like the
bottleneck*.

A free-running top has no control interface, but each `hls::task` instance inside it is an ordinary
`ap_ctrl_hs` block with `ap_start`/`ap_continue` tied to `1'b1` — so `ap_done` still pulses once per
firing, and HLS holds it until the firing's outstanding writes have responded.

**Read backpressure from the FIFO's occupancy, not its write enable.** HLS *gates* the write enable:
a task blocked on a full channel stalls its pipeline without ever asserting `write`, so a
`write & !full_n` metric reports zero backpressure while the producer is stuck. `blocked` compares
`<ch>_num_data_valid` against `<ch>_fifo_cap` instead. This is what located the 30 cycles.

## What this is for

The measured law for `mem_copy`'s writer, across `n_words` from 32 to 512:

```
span = 41 + n + 2 × (ceil(n/16) − 1)
```

One cycle per word, **two cycles per AXI burst boundary** (HLS's `max_burst_length` is 16), plus a
fixed control cost. The burst term belongs on `BusTiming` (the memory slave); the 41 belongs to the
component.

Feeding those back into the SimPy model — and giving the internal channels their real depth of 2 —
reproduces the RTL across that whole 16× range to **1.1%**, with the 30 cycles of congestion
emerging rather than being fitted. Implementing that in the framework, and automating the fit, is
the next arc: see `plans/memcpy_timing_calibration.md`.

## See also

- [RTL simulation](./rtlsim.md) — the run this instruments
- [Testbench codegen](./codegen_tb.md) — the harness the trace observes
- `waveflow/utils/trace.py` — `load_trace`, `BoundTrace`, `Firing`
- `waveflow/build/trace_steps.py` — the four steps
