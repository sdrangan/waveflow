---
title: Composite kernels
parent: Resource analysis
nav_order: 3
has_children: false
audience: python
api: [attribute_resources, report_from_solution, SynthReport, ModuleResources, UnmappedModuleError]
summary: "Decomposing a multi-task kernel's area into the parts that caused it. Two traps first: the report's rows NEST (a function row already contains its pipelined loops, so summing every row double-counts), and a whole class of generated RTL — the m_axi adapters, the inter-task FIFOs, the AXI-Lite control — gets no row at all. What is left after the modules are claimed is the interface term, and it is in one-to-one correspondence with the elaborated interface graph rather than being a residue."
---

# Composite kernels

A [free-running composite](../flows/concurrent.md) synthesizes as one Vitis project: several
`hls::task` bodies plus the plumbing between them. The report gives one design total and a table of
per-entity rows, and the natural question is *which part of my design cost what?*

That question is answerable, but the report will mislead you twice on the way if you take it at face
value.

Worked example throughout: `examples/fir_block`, a composite of four modules —
`FirCmdRx → MemRStream → FirCompute → MemWStream`.

## What actually gets a row

Vitis reports a row per module it derived from a **C function**, plus a row per **loop it hoisted into
its own pipeline module**, plus the DATAFLOW entry process and the top:

```text
entry_proc                                    the DATAFLOW entry process
fir_cmd_rx_task_32_s                          a task  (C function)
mem_r_stream_framed_task_32_s                 a task
  mem_r_stream_framed_task_32_Pipeline_RELAY    ...its RELAY loop
  mem_r_stream_framed_task_32_Pipeline_A2S      ...its A2S loop
fir_compute_unroll_task_32_s                  a task
  fir_compute_unroll_task_32_Pipeline_FIRV      ...its FIRV loop
  fir_compute_unroll_task_32_Pipeline_LOAD      ...
  fir_compute_unroll_task_32_Pipeline_SAVE      ...
mem_w_stream_framed_done_task_32_8_s          a task
  ...three more Pipeline_ rows
fir_block                                     the top — the design total
```

The `_Pipeline_<label>` suffix is the **C++ loop label**, so every row traces back to source.

## Trap 1: the rows nest

```text
fir_compute_unroll_task_32_s             LUT 5980   FF 10480   DSP 64
fir_compute_unroll_task_32_Pipeline_FIRV LUT 4312   FF  7725   DSP 64
```

The function row **already contains** its loop rows. `fir_compute_unroll_task_32_s` costs 5980 LUT
total — not 5980 + 4312 + …

{: .warning }
> Summing every row in `module_info` double-counts. On this design it nearly doubles the compute module
> and pushes the "total" past the real one — which at least fails visibly. A shallower nesting would
> not, and would quietly inflate everything built on it.

Sum **task rows only**; keep the loop rows as breakdown attached to the task they belong to.

## Trap 2: a whole class of RTL has no row

Compare the report's rows against the Verilog Vitis actually generated, and twelve entities appear in
the netlist and nowhere in the table:

```text
control_s_axi          gmem0_m_axi        fifo_w33_d2_S     regslice_both
flow_control_...init   gmem1_m_axi        fifo_w64_d3_S     sparsemux_17_3_32
mul_24s_24s_46         carry_RAM_AUTO     fifo_w64_d5_S     sparsemux_63_5_24
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

On `fir_block` at `ntap=32, samp_w=16`, serial:

| | LUT | FF | DSP | BRAM |
|---|---|---|---|---|
| Σ modules | 6690 | 9398 | 32 | 0 |
| interface + shell | 1984 | 1949 | **0** | **2** |
| design total | 8674 | 11347 | 32 | 2 |

Two things are legible immediately. **DSP is perfectly additive** — all 32 in the compute, one per tap,
nothing in the glue. And **BRAM is entirely glue** — no module row carries any, because the tap storage
went to LUT/FF under `ARRAY_PARTITION`.

### The third term is not a residue

It comes out by subtraction only because Vitis declines to itemize it. It is in **one-to-one
correspondence with the design's interface graph**:

| unreported RTL | comes from | count here |
|---|---|---|
| `gmem<n>_m_axi` | one per `m_axi` boundary port (`m_in`, `m_out`) | 2 |
| `fifo_w<W>_d<D>_S` | one per **internal** task-to-task channel | 3 |
| `control_s_axi` | the ap_ctrl / AXI-Lite block | 1 |
| `entry_proc`, `regslice_both`, `sparsemux_*` | the DATAFLOW shell | fixed |

Note the FIFOs are *internal*: the term depends on the whole interface graph, not just the outward-
facing port list. And none of it depends on what the modules compute — which is why, across a 24-point
sweep of `ntap × samp_w × realization`, the term was **identical at every point**
(`lut 1984, ff 1949, dsp 0, bram 2`). Changing `mem_dwidth` — which changes adapter and FIFO widths —
is what would move it.

{: .note }
> The term can be **negative** if HLS shared logic across module boundaries. That is information, not
> an error: it is exactly the cross-block surprise that whole-design synthesis exists to catch, and it
> is invisible if modules are only ever measured standalone.

## Doing it in Python

```python
from waveflow.build.elaborate import elaborate
from waveflow.calib.synth_report import report_from_solution
from examples.fir_block.fir_block import FirBlock

top = elaborate(FirBlock, {"mem_dwidth": 32, "ntap": 32, "samp_w": 16,
                           "samp_i": 2, "unroll_lane": False}, name="fir_block")
report = report_from_solution(top, "examples/fir_block/fir_block_proj/solution1")

for m in report.modules:
    print(f"{m.cls_name:12s} {m.rtl_module:38s} {m.resources}")
print("Σ modules  ", report.module_sum)
print("interface  ", report.integration)
print("total      ", report.top)
print("no module  ", list(report.unclaimed))     # entry_proc
```

The elaborated top is what the report is attributed *against* — the same parameters that generated the
C++ are used to read its report back, so the module graph cannot drift from the design that was
synthesized. Matching is by RTL prefix, derived from each module's own
[`KernelTask`](../comp_codegen/freerunning_override.md) (`task_fn` + `template_args` →
`mem_w_stream_framed_done_task_32_8`), so nothing is tabulated by hand.

{: .note }
> **The parameters must be the ones that were synthesized.** A solution directory holds whatever was
> built last, so passing a different configuration is an error, not a silent mismatch:
>
> ```text
> UnmappedModuleError: could not map every module onto the synthesis report:
>   FirCompute (fir_block.fir_block_compute) expected an RTL row starting 'fir_compute_serial_task_32'
> RTL rows present: ['entry_proc', 'fir_block', 'fir_compute_unroll_task_32_s', ...]
> ```
>
> Here the project on disk was built with `unroll_lane=True` and the query asked for the serial kernel.
> The error names both what it wanted and what was there — which is the whole reason attribution
> derives the expected name from the module rather than accepting whatever row looks close.

{: .warning }
> A module with **no** matching row raises `UnmappedModuleError` rather than being skipped. Dropping
> one shrinks the module sum and therefore *inflates* the interface term — which reads as "the modules
> are well modelled and the glue is expensive" when the truth is "we lost a module". Rows belonging to
> no module (`entry_proc`) are kept in `report.unclaimed` rather than discarded, so the arithmetic
> stays inspectable.

## See also

- [Reading the report](./parser.md) — the parser underneath.
- [Resource measurements](../calib/resources.md) — filing these numbers per module so a later design
  reuses them instead of re-synthesizing.
