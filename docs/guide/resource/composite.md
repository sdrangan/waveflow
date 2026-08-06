---
title: Composite kernels
parent: Resource Analysis Tools
nav_order: 3
has_children: false
audience: python
api: [attribute_resources, report_from_solution, SynthReport, ModuleResources, UnmappedModuleError]
summary: "Decomposing a multi-task kernel's utilization into the parts that caused it. report_from_solution does it against the elaborated module graph; this page explains why that is not the same as summing the report's rows. The rows NEST (a function row already contains its pipelined loops), and a whole class of generated RTL — the m_axi adapters, the inter-task FIFOs, the AXI-Lite control — gets no row at all. What is left after the modules are claimed is the integration term, in one-to-one correspondence with the elaborated interface graph rather than being a residue."
---
# Composite kernels

A [free-running composite](../flows/concurrent.md) synthesizes as **one** Vitis project, and therefore
produces **one** `csynth.xml`. Inside it is a design total and a table of per-entity rows, which raises
the obvious question: *which part of my design cost what?*

That question is answerable, and Waveflow answers it for you. It is worth knowing why it is not simply
a matter of reading the table, because the report will mislead you twice if you take it at face value.

As an example, the block FIR (`examples/fir_block`) is a composite of **four** sub-modules in a chain:

```text
FirCmdRx  →  MemRStream  →  FirCompute  →  MemWStream
```

Every figure on this page comes from its committed synthesis at `mem_dwidth=64, ntap=32, samp_w=16`
with the unrolled compute kernel.

## Getting the per-module breakdown

This reads a report that already exists, so **run C-synthesis first** — with
[`CSynthStep`](../build/vitis.md#csynthstep) in a build DAG, or `vitis_hls` directly. That leaves a
[solution directory](./parser.md#c-synthesis-outputs) on disk, and that path is what you point at.

`report_from_solution` then attributes that report to the modules of an **elaborated** top — the module
graph built from the class and its `HwParam` values alone, with no simulation:

```python
from waveflow.build.elaborate import elaborate
from waveflow.calib.synth_report import report_from_solution
from examples.fir_block.fir_block import FirBlock

top = elaborate(FirBlock, {"mem_dwidth": 64, "ntap": 32, "samp_w": 16,
                           "samp_i": 2, "unroll_lane": True}, name="fir_block")
report = report_from_solution(top, "examples/fir_block/fir_block_proj/solution1")

for m in report.modules:
    print(f"{m.cls_name:12s} {m.rtl_module:38s} lut={m.resources['lut']:6d} dsp={m.resources['dsp']}")
print("Σ modules  ", report.module_sum)
print("integration", report.integration)
print("total      ", report.top)
print("no module  ", list(report.unclaimed))
```

```text
FirCmdRx     fir_cmd_rx_task_64_s                   lut=   251 dsp=0
MemRStream   mem_r_stream_framed_task_64_s          lut=   824 dsp=0
FirCompute   fir_compute_unroll_task_64_s           lut=  6147 dsp=128
MemWStream   mem_w_stream_framed_done_task_64_8_s   lut=  2097 dsp=0
Σ modules    {'lut': 9319, 'ff': 12780, 'dsp': 128, 'bram': 0, 'uram': 0}
integration  {'lut': 2356, 'ff': 2057, 'dsp': 0, 'bram': 4, 'uram': 0}
total        {'lut': 11675, 'ff': 14837, 'dsp': 128, 'bram': 4, 'uram': 0}
no module    ['entry_proc']
```

The elaborated top is what the report is attributed *against*: the same parameters that generated the
C++ are used to read its report back, so the module graph cannot drift from the design that was
synthesized. Matching is by RTL prefix derived from each module's own
[`KernelTask`](../comp_codegen/freerunning_override.md) (`task_fn` + `template_args` →
`mem_w_stream_framed_done_task_64_8`), so **no name is tabulated by hand** — which is the part you
would otherwise have to be careful about.

Each module also carries its own loop-level breakdown in `m.subblocks`, as figures *within* its total
rather than additions to it — see [Trap 1](#trap-1-the-rows-nest).

## You get this on every build

Nothing above needs doing by hand in a build DAG.
[`InspectSynthStep`](../build/vitis.md) is the rung after `csynth`: it consumes the `report_dir`
artifact, attributes it, writes `results/resources.json`, and files each module's figures into the
platform's [module store](../resource_model/resources.md).

```python
dag.add(InspectSynthStep(
    name="resources", comp_class=FirBlock, top_name=TOP,
    elaborate_params=("mem_dwidth", "ntap", "samp_w", "samp_i", "unroll_lane"),
    params={"mem_dwidth": MEM_DW, "ntap": DEFAULT_NTAP, "samp_w": DEFAULT_SAMP_W,
            "samp_i": DEFAULT_SAMP_I, "unroll_lane": False}))
```

The synthesis is the expensive part; attributing its report costs a tenth of a second. That is why it
runs on **every** C-synthesis rather than only when someone remembers to calibrate — the numbers above
are read back from the `results/resources.json` this step wrote.

## Why this is not just summing the rows

### What actually gets a row

Vitis reports a row per module it derived from a **C function**, plus a row per **loop it hoisted into
its own pipeline module**, plus the DATAFLOW entry process and the top:

```text
entry_proc                                    the DATAFLOW entry process
fir_cmd_rx_task_64_s                          a task  (C function)
mem_r_stream_framed_task_64_s                 a task
  mem_r_stream_framed_task_64_Pipeline_RELAY    ...its RELAY loop
  mem_r_stream_framed_task_64_Pipeline_A2S      ...its A2S loop
fir_compute_unroll_task_64_s                  a task
  fir_compute_unroll_task_64_Pipeline_FIRV      ...its FIRV loop
  fir_compute_unroll_task_64_Pipeline_LOAD      ...
  fir_compute_unroll_task_64_Pipeline_SAVE      ...
mem_w_stream_framed_done_task_64_8_s          a task
  ...three more Pipeline_ rows
fir_block                                     the top — the design total
```

The `_Pipeline_<label>` suffix is the **C++ loop label**, so every row traces back to source.

### Trap 1: the rows nest

```text
fir_compute_unroll_task_64_s             LUT 6147   FF 9754   DSP 128
fir_compute_unroll_task_64_Pipeline_FIRV LUT 4387   FF 7627   DSP 128
```

The function row **already contains** its loop rows. `fir_compute_unroll_task_64_s` costs 6147 LUT
total — not 6147 + 4387 + …

{: .warning }
> Summing every row double-counts. On this design it nearly doubles the compute module and pushes the
> "total" past the real one — which at least fails visibly. A shallower nesting would not, and would
> quietly inflate everything built on it.

Sum **task rows only**; keep the loop rows as breakdown attached to the task they belong to. That is
what `ModuleResources.resources` vs `.subblocks` encodes.

### Trap 2: a whole class of RTL has no row

Compare the report's 14 rows against the 27 Verilog entities Vitis actually generated, and **thirteen**
appear in the netlist and nowhere in the table:

```text
control_s_axi                gmem0_m_axi    fifo_w64_d3_S   regslice_both
flow_control_..._init        gmem1_m_axi    fifo_w64_d5_S   sparsemux_17_3_64
mac_muladd_16s_16s_30s_30    carry_RAM_AUTO fifo_w65_d2_S   sparsemux_71_6_16
mul_16s_16s_30
```

The rule is simple once seen: **Vitis reports what it derived from C functions.** Everything it
generated from *interface pragmas and dataflow channels* — the AXI adapters, the AXI-Lite control
block, the channel FIFOs, register slices, shared arithmetic and mux primitives — gets no row.

## The three terms

So a composite's total decomposes as:

```text
    design total  =  Σ task modules  +  interface logic  +  shell
                     └── reported ──┘  └──── not reported, by subtraction ────┘
```

|                                 | LUT   | FF    | DSP         | BRAM        |
| ------------------------------- | ----- | ----- | ----------- | ----------- |
| Σ modules                      | 9319  | 12780 | 128         | 0           |
| integration (interface + shell) | 2356  | 2057  | **0** | **4** |
| design total                    | 11675 | 14837 | 128         | 4           |

Two things are legible immediately. **DSP is perfectly additive** — all 128 sit in the compute module,
none in the glue. And **BRAM is entirely glue** — no module row carries any, because the tap storage
went to LUT/FF under `ARRAY_PARTITION`.

### The third term is not a residue

It comes out by subtraction only because Vitis declines to itemize it. It is in **one-to-one
correspondence with the design's interface graph**:

| unreported RTL                                     | comes from                                           | count here |
| -------------------------------------------------- | ---------------------------------------------------- | ---------- |
| `gmem<n>_m_axi`                                  | one per `m_axi` boundary port (`m_in`, `m_out`) | 2          |
| `fifo_w<W>_d<D>_S`                               | one per **internal** task-to-task channel       | 3          |
| `control_s_axi`                                  | the ap_ctrl / AXI-Lite block                         | 1          |
| `entry_proc`, `regslice_both`, `sparsemux_*` | the DATAFLOW shell                                   | fixed      |

Three internal FIFOs for a four-module chain, and their widths are legible too: `w65` is a 64-bit word
plus the one framing bit an [in-band framed](../flows/concurrent.md) edge carries.

Note the FIFOs are *internal*: the term depends on the whole interface graph, not just the outward-
facing port list. And none of it depends on what the modules compute. Across the committed 24-point
sweep of `ntap × samp_w × realization` (`results/sweep.json`), the term is **identical at every single
point**:

```text
lut 1984   ff 1949   dsp 0   bram 2        # all 24 points, mem_dwidth = 32
```

What *does* move it is changing a width the adapters and FIFOs are built from
(`results/sweep_memdw.json`):

| `mem_dwidth` | LUT  | FF   | DSP | BRAM |
| -------------- | ---- | ---- | --- | ---- |
| 32             | 1984 | 1949 | 0   | 2    |
| 64             | 2356 | 2057 | 0   | 4    |

Both `unroll_lane` settings give the same term at each width, which is the claim stated precisely: the
integration term is a function of the **interface graph**, not of the modules hanging off it.

{: .note }
> The term can be **negative** if HLS shared logic across module boundaries. That is information, not
> an error: it is exactly the cross-block surprise that whole-design synthesis exists to catch, and it
> is invisible if modules are only ever measured standalone.

## When attribution fails

{: .note }
> **The parameters must be the ones that were synthesized.** A solution directory holds whatever was
> built last, so passing a different configuration is an error, not a silent mismatch:
>
> ```text
> UnmappedModuleError: could not map every module onto the synthesis report:
>   FirCompute (fir_block.fir_block_compute) expected an RTL row starting 'fir_compute_serial_task_64'
> RTL rows present: ['entry_proc', 'fir_block', 'fir_compute_unroll_task_64_s', ...]
> ```
>
> Here the project on disk was built with `unroll_lane=True` and the query asked for the serial kernel.
> The error names both what it wanted and what was there — which is the whole reason attribution
> derives the expected name from the module rather than accepting whatever row looks close.

{: .warning }
> A module with **no** matching row raises rather than being skipped. Dropping one shrinks the module
> sum and therefore *inflates* the integration term — which reads as "the modules are well modelled and
> the glue is expensive" when the truth is "we lost a module". Rows belonging to no module
> (`entry_proc`) are kept in `report.unclaimed` rather than discarded, so the arithmetic stays
> inspectable.

## See also

- [Reading the report](./parser.md) — the parser underneath.
- [Resource measurements](../resource_model/resources.md) — filing these numbers per module so a later design
  reuses them instead of re-synthesizing.
- [Resource models](../resource_model/) — using them to predict a configuration you have not
  synthesized.
