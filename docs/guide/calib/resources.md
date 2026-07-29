---
title: Resource measurements
parent: Model calibration
nav_order: 11
has_children: false
audience: python
api: [CsynthParser, attribute_resources, report_from_solution, SynthReport, InspectSynthStep, store_report]
summary: "Turn a Vitis C-synthesis report into per-module resource records. The report already carries a per-RTL-module breakdown, so attribution needs no standalone harness — only the whole-top csynth the build already runs. Two traps decide whether the numbers mean anything: the report is HIERARCHICAL (a task row already contains its _Pipeline_ children, so summing every row double-counts), and module names are mangled but derivable from each module's own KernelTask. What is left after every module is claimed is the integration term, reported separately and never folded in."
---

# Resource measurements

Timing is calibrated from a run — a cosim or an [XSI](../build/xsi.md) trace. Resources come from a
report instead: Vitis writes `csynth.xml` at the end of C-synthesis, and it already contains both the
design total and a **per-RTL-module breakdown**.

That breakdown is worth more than it first appears. It means per-module resource attribution needs
**no standalone synthesis harness** — only the whole-top `csynth` a build already runs. Everything
expensive has already happened; this is reading its output.

## Reading the report: `CsynthParser`

The low-level parser is [`CsynthParser`](../build/vitis.md), which also exposes `loop_df` (pipeline
II / depth per loop) and `res_df`:

```python
from waveflow.utils.csynthparse import CsynthParser

p = CsynthParser(sol_path="examples/fir_block/fir_block_proj/solution1")
p.get_total_resources()      # -> p.total_resources, p.available_resources
p.get_module_resources()     # -> p.module_info: {rtl_entity: {...}}
```

```text
TOTAL    : {'BRAM_18K': 2, 'DSP': 32, 'FF': 11347, 'LUT': 8674, 'URAM': 0}
AVAILABLE: {'BRAM_18K': 280, 'DSP': 220, 'FF': 106400, 'LUT': 53200, 'URAM': 0}
```

Using it directly is fine for a one-off inspection. For anything you want to *keep*, use the
attribution layer below — because two properties of the raw report will otherwise quietly corrupt
the numbers.

## Trap 1: the report is hierarchical

This is the one that bites. Here are four of the rows from the same report:

```text
fir_compute_serial_task_32_s                LUT 3728   FF 7355   DSP 32
fir_compute_serial_task_32_Pipeline_FIR     LUT 2554   FF 5368   DSP 32
fir_compute_serial_task_32_Pipeline_LOAD    LUT  167   FF   98   DSP  0
mem_r_stream_framed_task_32_s               LUT  833   FF  472   DSP  0
```

The `_Pipeline_*` rows are **sub-blocks of the task above them**, and the parent's figure *already
contains* them. `fir_compute_serial_task_32_s` costs 3728 LUT total — not 3728 + 2554 + 167.

{: .warning }
> Summing every row in `module_info` double-counts. On this design it nearly doubles the compute
> module, and the resulting "total" exceeds the design total — which at least fails visibly. A shallower
> nesting would not, and would just quietly inflate every estimate built on it.

Only **task rows** are summed. Sub-blocks are retained as breakdown, on the module they belong to.

## Trap 2: names are mangled — but derivably

An RTL entity is `<task_fn>_<template args joined by _>` plus a tool suffix. Both halves come from the
module's own [`KernelTask`](../comp_codegen/freerunning_override.md), so nothing is tabulated by hand:

| module | `task_fn` | `template_args` | RTL entity |
|---|---|---|---|
| `FirCmdRx` | `fir_cmd_rx_task` | `(32,)` | `fir_cmd_rx_task_32_s` |
| `MemWStream` | `mem_w_stream_framed_done_task` | `(32, 8)` | `mem_w_stream_framed_done_task_32_8_s` |

The suffix is deliberately not predicted — it is a Vitis convention we do not control, so matching is
by prefix.

{: .warning }
> A module with **no** matching row raises `UnmappedModuleError` rather than being skipped. Dropping
> one shrinks the per-module sum and therefore *inflates* the integration term below — which reads as
> "the modules are well modelled and the glue is expensive" when the truth is "we lost a module".
> Every other error here is recoverable; this one silently corrupts the conclusion.

## Attribution

```python
from waveflow.build.elaborate import elaborate
from waveflow.calib.synth_report import report_from_solution
from examples.fir_block.fir_block import FirBlock

top = elaborate(FirBlock, {"mem_dwidth": 32, "ntap": 32, "samp_w": 16,
                           "samp_i": 2, "unroll_lane": False}, name="fir_block")
report = report_from_solution(top, "examples/fir_block/fir_block_proj/solution1")
```

The elaborated top is what the report is attributed *against*: the same parameters that generated the
C++ are used to read its report back, so the module graph cannot drift from the design that was
synthesized.

```python
for m in report.modules:
    print(f"{m.cls_name:12s} {m.rtl_module:38s} {m.resources}")
print("Σ modules  ", report.module_sum)
print("integration", report.integration)
print("top        ", report.top)
```

```text
FirCmdRx     fir_cmd_rx_task_32_s                   lut=279  ff=107   dsp=0
MemRStream   mem_r_stream_framed_task_32_s          lut=833  ff=472   dsp=0    (+2 subblocks)
FirCompute   fir_compute_serial_task_32_s           lut=3728 ff=7355  dsp=32   (+2 subblocks)
MemWStream   mem_w_stream_framed_done_task_32_8_s   lut=1850 ff=1464  dsp=0    (+3 subblocks)
Σ modules    lut=6690  ff=9398   dsp=32  bram=0
integration  lut=1984  ff=1949   dsp=0   bram=2
top          lut=8674  ff=11347  dsp=32  bram=2
```

### The third term

`integration` is `top − Σ modules`: everything the design costs that no module accounts for — the
interconnect, the inter-task FIFOs, the DATAFLOW `entry_proc`, and anything HLS shared across task
boundaries. It is reported separately and **never folded into a module**, because the question a
resource model exists to answer is whether

```text
    design total  ≈  Σ modules  +  interfaces  +  shell
```

and folding would assume the answer. Rows that belong to no module (`entry_proc`) are kept in
`report.unclaimed` rather than dropped, so the arithmetic stays inspectable.

{: .note }
> `integration` can be **negative** if HLS shared logic across module boundaries. That is information,
> not an error — it is precisely the cross-block surprise that whole-top runs exist to catch, and it
> would be invisible if per-module numbers were only ever measured standalone.

Two things are already visible in the numbers above. **DSP is perfectly additive** — all 32 sit in
`FirCompute`, one per tap, with an integration term of exactly zero. And **BRAM is entirely
integration** — no module row reports any; the design's two blocks appear only on the top row.

### What is actually in the term

The report does not break the top row down further, but the generated RTL names its contents. For
`fir_block`, everything that is not a task module is:

```text
fir_block.v                       top-level wiring
fir_block_control_s_axi.v         the AXI-Lite control interface
fir_block_entry_proc.v            the DATAFLOW entry process
fir_block_gmem0_m_axi.v           the m_axi adapters — one per bundle, and
fir_block_gmem1_m_axi.v             typically the bulk of the term
fir_block_fifo_w33_d2_S.v         the inter-task channels, one per hls::stream
fir_block_fifo_w64_d3_S.v
fir_block_fifo_w64_d5_S.v
fir_block_regslice_both.v         register slices, flow control, shared muxes …
```

Every one of those is a function of the **boundary** — how many `m_axi` bundles, how wide, how many
internal streams — and none of them is a function of what the modules compute. That is why the term is
invariant under `ntap`, `samp_w`, and the realization knob, and why it is modelled as an *interface +
shell* term keyed on boundary structure rather than fit against the compute parameters.

{: .note }
> Which of those holds the 2 BRAM is **not** established by the report — `FIFOInformation` is empty
> and no sub-module row carries it. The `m_axi` adapters' internal buffers are the likely home (the
> term is identical in the serial variant, which has no state RAM at all), but that is inference, not
> measurement.

## The build step

`InspectSynthStep` is the DAG rung after `csynth`. It attributes the report, writes it as JSON, and
files the records into the platform's module store:

```python
from waveflow.build.resource_steps import InspectSynthStep

dag.add(CSynthStep(name="csynth"))
dag.add(InspectSynthStep(
    name="resources", comp_class=FirBlock, top_name="fir_block",
    elaborate_params=("mem_dwidth", "ntap", "samp_w", "samp_i", "unroll_lane"),
    params={"mem_dwidth": 32, "ntap": 32, "samp_w": 16, "samp_i": 2, "unroll_lane": False}))
```

`elaborate_params` names which of the step's params are *elaboration* parameters — the rest (a
`live_output` flag, say) are the step's own business and must not reach `elaborate`.

Records land only when the build selects a [platform](./platform.md); without one the step writes
`results/resources.json` and says so. That is deliberate — plenty of builds synthesize without wanting
to publish anything, and a step that reads a report must never fail a build that synthesized correctly.

### Recording what it cost

The producing step publishes its own wall-clock as an artifact:

```python
    produces = {"report_dir": Path("fir_block_proj/solution1"), "synth_seconds": None}
```

`InspectSynthStep` consumes it and stamps it into each record's `cost_seconds`, split evenly across
the modules — the synthesis was one indivisible run, and pretending to know each module's share of it
would be inventing data. That history is what later answers *"what would recalibrating here cost?"*,
which is a better question to answer from real runs than from an estimate, and unanswerable after the
fact if never measured.

## Sweeping

`examples/fir_block/fir_block_sweep.py` drives the grid, one csynth per point:

```bash
python -m examples.fir_block.fir_block_sweep --dry-run   # elaborate + codegen only, no Vitis
python -m examples.fir_block.fir_block_sweep             # the full sweep
python -m examples.fir_block.fir_block_sweep --resume    # continue an interrupted one
```

Three habits worth copying into any sweep of your own:

- **Pre-flight the whole grid without the toolchain first.** `--dry-run` runs every point through
  codegen in about a second. Learning that a parameter combination does not generate is worth one
  second, not two hours.
- **Write incrementally and support resume.** Hours of synthesis should not be lost to one crash near
  the end.
- **Record failures as failures.** A sweep that quietly covered 19 of 24 points while reporting 24
  leaves a hole in the fitted region — exactly where [confidence](./modules.md) would later report
  interpolation.

{: .warning }
> A sweep writes the **work tier** (`calib/work/<name>`), never the tracked library. Give it its own
> platform name: reusing a shipped platform's name makes `Platform.resolve` find the *packaged*
> directory through its fallbacks and write there, and only `publish_calib` may do that. See
> [the calibration workflow](./workflow.md).

## See also

- [Module keys and the record store](./modules.md) — how a measurement is addressed and filed.
- [Vitis build primitives](../build/vitis.md) — `run_vitis_hls`, `CsynthParser`, the step shapes.
- [Platforms](./platform.md) — resource counts are part- and clock-specific, and keyed accordingly.
