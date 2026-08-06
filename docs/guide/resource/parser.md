---
title: Reading the report — CsynthParser
parent: Resource Analysis Tools
nav_order: 2
has_children: false
audience: python
api: [CsynthParser]
summary: "The parser over a Vitis solution's csynth.xml: get_total_resources() for the design total and the device capacity, get_module_resources() for the per-RTL-module breakdown, and get_loop_pipeline_info() for per-loop II / depth / trip count. Documents the quirks — get_resources() returns None and populates res_df, and Vitis's own top-row total can disagree with its AreaEstimates total by a couple of LUTs."
---

# Reading the report — `CsynthParser`

## C-synthesis outputs

**C-synthesis** is the first stage of the Vitis HLS flow: it compiles a C++ kernel into RTL, scheduling
operations into clock cycles and binding each one to a physical primitive
([FPGA resources](./xilinx.md)). Those binding decisions are what the utilization numbers on this page
report, which is why they are available in seconds to minutes, before Vivado has run.

Each synthesis writes its outputs into a **solution directory**. Waveflow's generated TCL names the
project and the solution, so the layout is fixed rather than something to go hunting for:

```text
<build root>/<top>_proj/          # open_project  -reset <top>_proj
└── solution1/                    # open_solution -reset "solution1"
    └── syn/
        ├── verilog/              # the generated RTL
        └── report/
            └── csynth.xml        # the machine-readable report — what this page parses
```

Better still, nothing in a build DAG needs to hardcode that path.
[`CSynthStep`](../build/vitis.md#csynthstep) publishes the solution directory as its **`report_dir`**
artifact, and every downstream step — resource attribution, cosim parsing, the XSI gate — consumes it
by name. Write a step that takes `report_dir`; reach for a literal path only when inspecting a
solution interactively, as below.

{: .note }
> `report_dir` is a *solution* directory, not the `syn/report` directory inside it — the name is
> historical. Each consumer appends the subpath it needs (`syn/report` here, `sim/report` for cosim).
> It is also hardcoded per example, so a DAG whose TCL uses different project or solution names must
> update `produces` to match.

### What is in `csynth.xml`

Vitis writes one `csynth.xml` per synthesis, and it covers considerably more than resources:

| section | what it holds |
|---|---|
| `UserAssignments` | the part, top name, target clock period and clock uncertainty |
| `PerformanceEstimates` | estimated clock period, overall latency and initiation interval, violations |
| `AreaEstimates` | `Resources` (what this design uses) and `AvailableResources` (what the part has) |
| `ModuleInformation` | both of the above *per RTL entity*, plus each module's loop table |
| `InterfaceSummary` | the generated RTL ports, grouped by bundle |

`CsynthParser` covers the resource and loop portions — `AreaEstimates` and `ModuleInformation` — which
is what the rest of this page walks through.

{: .warning }
> Every number here is an **estimate**, produced before logic synthesis and place and route. DSP and
> BRAM counts track the final result well because they reflect binding decisions HLS made explicitly;
> LUT and FF can move substantially once Vivado optimizes. See
> [the estimate precedes Vivado](./xilinx.md#how-vitis-produces-the-estimate) for how far to trust
> each counter.

## Parsing the report

Waveflow provides [`CsynthParser`](../build/vitis.md) to read those outputs. Point it at a solution
directory:


```python
from waveflow.utils.csynthparse import CsynthParser

p = CsynthParser(sol_path="examples/fir_block/fir_block_proj/solution1")
# or point at the report directory directly:
# p = CsynthParser(report_path=".../solution1/syn/report")
```

`sol_path` expects the solution root and appends `syn/report` itself. Construction raises
`FileNotFoundError` if `csynth.xml` is not there, so a missing synthesis fails immediately rather than
producing empty tables.

## Totals and device capacity

```python
p.get_total_resources()
p.total_resources       # {'BRAM_18K': 4, 'DSP': 128, 'FF': 14837, 'LUT': 11675, 'URAM': 0}
p.available_resources   # {'BRAM_18K': 280, 'DSP': 220, 'FF': 106400, 'LUT': 53200, 'URAM': 0}
```

`total_resources` is the whole design. `available_resources` is the **part**, which is what makes a
utilization percentage meaningful and is worth recording alongside any measurement — the same LUT count
means different things on a Zynq-7020 and a Versal.

## The per-module breakdown

```python
p.get_module_resources()
p.module_info           # {rtl_entity_name: {'LUT': ..., 'FF': ..., 'AVAIL_LUT': ..., 'UTIL_LUT': ...}}
```

This is the interesting one, and the subject of [Composite kernels](./composite.md). Each key is an
**RTL entity**, not a C++ file or a Waveflow module. Values carry the raw report columns, including the
`AVAIL_*` / `UTIL_*` device context and the occasional `~0` string.

{: .warning }
> `module_info` is **not a partition of the total**. Rows nest (a function's row already contains its
> pipelined loops'), and a large class of generated RTL gets no row at all. Summing it is wrong in two
> different directions at once — see [Composite kernels](./composite.md).

## The loop table

```python
p.get_loop_pipeline_info()
p.loop_df               # a pandas DataFrame, one row per pipelined loop
```

```text
                                                    PipelineII  PipelineDepth  TripCountMin  TripCountMax  LatencyMin  LatencyMax
mem_r_stream_framed_task_64_Pipeline_RELAY:RELAY             1              1          <NA>          <NA>        <NA>        <NA>
mem_r_stream_framed_task_64_Pipeline_A2S:A2S                 1              3          <NA>          <NA>        <NA>        <NA>
fir_compute_unroll_task_64_Pipeline_FIRV:FIRV                1              9             0          2048           2        2055
fir_compute_unroll_task_64_Pipeline_SAVE:SAVE                1              2          <NA>          <NA>          31          31
fir_compute_unroll_task_64_Pipeline_LOAD:LOAD                1              2             0            32           0          32
mem_w_stream_framed_done_task_64_8_Pipeline_BUF...           1              1          <NA>          <NA>        <NA>        <NA>
mem_w_stream_framed_done_task_64_8_Pipeline_S2A...           1              3          <NA>          <NA>        <NA>        <NA>
mem_w_stream_framed_done_task_64_8_Pipeline_ECH...           1              2          <NA>          <NA>        <NA>        <NA>
```

The index is `<rtl entity>:<C++ loop label>`, so a loop is traceable back to the source label that
named it. This is a **timing** table living in the utilization report, and it is the quickest check that
pipelining did what you intended: an `II` above 1 on the datapath loop is usually a dependency you did
not mean to create.

```python
# the standard smoke test after a synthesis
bad = p.loop_df[p.loop_df["PipelineII"] > 1]
if not bad.empty:
    raise RuntimeError(f"loops failed to pipeline at II=1:\n{bad}")
```

`<NA>` appears where the report omits a field — typically a loop with no statically-known trip count
(a `while` over a stream). `PipelineII` and `PipelineDepth` are nullable integers, so compare with
care.

## The summary frame

```python
p.get_resources()       # returns None -- populates p.res_df
p.res_df
```

```text
                                                  BRAM_18K  DSP      FF    LUT  URAM
entry_proc                                               0    0       2     29     0
fir_cmd_rx_task_64_s                                     0    0     198    251     0
mem_r_stream_framed_task_64_Pipeline_RELAY               0    0      34    157     0
mem_r_stream_framed_task_64_Pipeline_A2S                 0    0     102    174     0
mem_r_stream_framed_task_64_s                            0    0     502    824     0
fir_compute_unroll_task_64_Pipeline_FIRV                 0  128    7627   4387     0
fir_compute_unroll_task_64_Pipeline_SAVE                 0    0      29    233     0
fir_compute_unroll_task_64_Pipeline_LOAD                 0    0     100    245     0
fir_compute_unroll_task_64_s                             0  128    9754   6147     0
mem_w_stream_framed_done_task_64_8_Pipeline_BUFF         0    0     642   1310     0
mem_w_stream_framed_done_task_64_8_Pipeline_S2A          0    0     100    139     0
mem_w_stream_framed_done_task_64_8_Pipeline_ECHO         0    0      98    168     0
mem_w_stream_framed_done_task_64_8_s                     0    0    2326   2097     0
fir_block                                                4  128   14837  11677     0
Total                                                    4  128   14837  11675     0
Available                                              280  220  106400  53200     0
```

One tidy table of just the counters, with `Total` and `Available` appended as rows. The nesting is
plainly visible here: `fir_compute_unroll_task_64_s` (6147 LUT) sits alongside its own three
`_Pipeline_` rows, whose figures are already inside it. Convenient for eyeballing, but for reading
rather than summing.

{: .note }
> **Two quirks worth knowing before they look like bugs.**
>
> `get_resources()` returns `None` and populates `p.res_df` as a side effect — assigning its return
> value gets you nothing.
>
> The `fir_block` row (11677 LUT) and the `Total` row (11675 LUT) **disagree by 2**, while FF, DSP and
> BRAM agree exactly. Vitis derives them slightly differently. Waveflow uses `AreaEstimates` (the
> `Total` row) as the design total, consistently, so figures elsewhere in these docs are on that basis.

## See also

- [FPGA resources](./xilinx.md) — what the counters mean and how far to trust them.
- [Composite kernels](./composite.md) — turning `module_info` into per-module numbers that add up.
