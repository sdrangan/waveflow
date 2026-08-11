---
title: Tracing a kernel run
parent: Timing Analysis Tools
nav_order: 9
has_children: false
---

# Tracing a kernel run — the four trace steps

To analyse a kernel's timing you need a **VCD of its internal signals**, and then a way to know
which net is which. Waveflow builds both as ordinary [BuildDag](../build/index.md) steps, so
instrumenting a run is one command rather than a procedure:

```bash
python <example>_build.py --through extract_bursts
```

```
CodegenTbStep → AddVcdTopStep + TraceManifestStep → RtlSimStep → ExtractBurstsStep
                (the dumper)     (the net names)     (the VCD)    (the timing table)
```

All four live in [`waveflow/build/trace_steps.py`](https://github.com/sdrangan/waveflow/tree/main/waveflow/build/trace_steps.py)
and are **general infrastructure** — the only example-specific input is the component class, passed
as a constructor argument. Any free-running (`ap_ctrl_none`) `hls::task` top produced by
`composite_top_spec` works unchanged.

> **Two ways to get a VCD.** [Extracting VCD Files](./vcd.md) (`xsim_vcd`) re-runs a **cosim** and
> injects `log_vcd` into the generated Tcl — the right tool for a control-driven (`ap_ctrl_hs`)
> kernel driven by `cosim_design`. The steps on *this* page are for the **free-running BFM/XSI**
> flow, where the kernel has no cosim wrapper and is driven cycle-by-cycle by a
> [BFM harness](../build/bfm.md). Same output format (a VCD the [parser](./parsing.md) reads); two
> different flows to produce it.

## The problem the dumper solves

The XSI value API (`xsi_get_port_number`) resolves **top-level ports only**. The interesting signals
in a composite kernel are usually *not* ports — they are the internal FIFOs wiring the tasks
together. Through XSI they are unreachable, and the BFM run writes a `.wdb`, not a VCD.

The way in is Verilog's `$dumpvars`, from a small module elaborated **alongside** the DUT.

## `AddVcdTopStep` — the dumper

Writes `xsi/vcd_dumper_<top>.v`:

```verilog
module vcd_dumper_<top>;
    initial begin
        $dumpfile("<top>_trace.vcd");
        $dumpvars(1, <top>);
    end
endmodule
```

Two decisions carry the weight.

**It is a second top, not a wrapper.** The runner's `trace` argument elaborates `work.<top>
work.vcd_dumper_<top>`. The DUT itself is untouched, so every BFM port number and every cycle count
is identical to an untraced run — the dumper *observes*, it does not perturb. (A pass-through wrapper
would also work, but means mirroring the whole port list by hand — tedium that fails for reasons
unrelated to what you are measuring.)

**The level is 1, not 0.** `$dumpvars(1, …)` dumps this scope's own signals and does **not** descend
into children. That sounds like it would miss the internal FIFOs — and it does not, because **Vitis
lifts inter-task dataflow channel wires up into the top scope**, beside the task instances. So a
level-1 dump already reaches, with **no hierarchical path to resolve**:

```
<ch>_dout / <ch>_empty_n / <ch>_full_n     the channel itself
<producer>_U0_<ch>_din / _<ch>_write        the producer's side
<consumer>_U0_<ch>_read                     the consumer's side
<task>_U0_ap_done                           each task's firing boundary
```

Level 0 would dump the entire subtree for no extra reach. The cost stays bounded — a level-1 dump is
258 signals for `mem_copy`, 363 for `interleaver_canon`, not the thousands a full-tree dump gives.

The module is named after the top because one `xsi/` directory can serve several tops
(`examples/interleaver/xsi` builds three), and a dumper naming a scope that is not part of *this*
elaboration is a hard error. The `trace` argument that selects it lives in the **master** runner —
[`run.bat`](https://github.com/sdrangan/waveflow/tree/main/waveflow/build/xsi/run.bat) on Windows,
[`run.sh`](https://github.com/sdrangan/waveflow/tree/main/waveflow/build/xsi/run.sh) on Linux —
which `XsiHarnessStep` copies into each example. Editing the copied script would be undone on the
next codegen.

## `TraceManifestStep` — what the nets are called

Writes `results/<top>_trace.json`, mapping the Python graph onto RTL net names. Derived entirely from
`elaborate()` — no RTL is read, nothing is simulated, so it is cheap and runs alongside the C++
codegen rather than after synthesis.

Why a manifest, instead of matching names in the waveform later? Because substring matching is not
merely fragile, it is **wrong**. An interleaver trace contains both

```
ywords_fifo_cap[2:0]                    the channel's own counter
il_store_..._U0_ywords_fifo_cap[31:0]   an instance's internal copy
```

— different widths, different meanings, and a matcher takes whichever it sees first. The names are
already known here, because **codegen chose them**: channel names are the C++ stream variables, port
names are the boundary names, `m_axi` nets are named after the bundle. Only the `_U0` instance suffix
is Vitis's. So the manifest binds *exactly*, and a net that has gone missing (a renamed channel, a
new Vitis release) **fails loudly** instead of silently extracting nothing.

The manifest is the artifact the loader ([`waveflow/utils/trace.py`](https://github.com/sdrangan/waveflow/tree/main/waveflow/utils/trace.py),
`load_trace` → `BoundTrace`) resolves against a VCD.

## `RtlSimStep` — run it, tracing

A thin wrapper over the example's XSI runner, passing its third argument `trace`. Which script that
is comes from `xsi_runner_cmd()` — `cmd /c run.bat` on Windows, `bash run.sh` on Linux. It
**asserts nothing**: it runs and produces `xsi/<top>_trace.vcd`. Correctness — the exact cycle
count — stays with the `-m xsi` gate, which calls the runner directly. Two callers of one script is deliberate; routing a
green gate through new code is how a gate quietly stops meaning what it meant.

{: .warning }
> **Re-running the built binary does not regenerate the VCD.** Only the full runner path does,
> because the dump comes from the *elaborated snapshot*. A driver that re-ran the binary per data
> point once silently re-measured the **previous** trace and reported an identical result at five
> different inputs. `RtlSimStep` deletes the VCD first and fails if it does not reappear — copy that
> discipline in any hand-rolled sweep.

It is toolchain-gated (needs Vitis-synthesized RTL plus Vivado `xsim` and a MinGW `g++`), and takes
a `prepare` hook to materialize the scenario the BFM reads — the scenario is an *input* to the run,
so a step that did not write it would measure whatever was left behind.

## `ExtractBurstsStep` — the timing table

Binds the manifest to the waveform and writes `results/<top>_timing.json` — one row per **firing** of
each component:

```json
{"component": "...", "index": 8,
 "start": 1488, "end": 1670, "span": 183,
 "nwords": 128, "num_trans": 8, "blocked": 0}
```

- `span` — first input handshake → `ap_done` (see [Trace pitfalls](./trace_pitfalls.md) for why this
  anchor and not the last output beat)
- `nwords` / `num_trans` — `m_axi` beats and bursts inside the firing, **measured** off the trace,
  not assumed
- `blocked` — cycles this component's output channel sat at capacity (from the FIFO's occupancy
  counters, the only reliable backpressure signal — again, [Trace pitfalls](./trace_pitfalls.md))

The shape is deliberate: `span` regressed over `{nwords, num_trans}` is exactly what a
[`BusTiming`](./aximm.md) model consumes, and `blocked == 0` isolates the firings that may be
calibrated on. So the calibration path consumes this artifact unchanged rather than needing a second
extractor.

## `BoundTrace` — reading a trace in Python

For interactive analysis you rarely need the steps; `load_trace` does the binding directly:

```python
from waveflow.utils.trace import load_trace

bt = load_trace("results/mem_copy_trace.json", "xsi/mem_copy_trace.vcd")

bt.component("mem_r_stream_framed_task")          # what streams it touches
bt.component_firings("mem_r_stream_framed_task")  # per-firing windows (ap_done anchored)
bt.component_bursts("mem_r_stream_framed_task")   # beats in/out per channel
bt.aximm_bursts("gmem1")                          # (write, read, clk_period) for a bundle
bt.channel_blocked("copy_data", lo, hi)           # backpressure cycles in a window
```

Every accessor takes the RTL instance name or the task-body name, and binds by exact net.

## See also

- [Trace pitfalls](./trace_pitfalls.md) — the three ways a trace measurement goes silently wrong
- [Parsing VCD Files](./parsing.md) — the `VcdParser` these build on
- [AXI4-MM](./aximm.md) / [AXI4-Stream](./axistream.md) — the burst extractors
- [memcpy timing](../../examples/memcpy/timing.md) — the whole flow applied to one kernel
