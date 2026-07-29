---
title: Reading the report — CsynthParser
parent: Resource analysis
nav_order: 2
has_children: false
audience: python
api: [CsynthParser]
summary: "The parser over a Vitis solution's csynth.xml: get_total_resources() for the design total and the device capacity, get_module_resources() for the per-RTL-module breakdown, and get_loop_pipeline_info() for per-loop II / depth / trip count. Documents the quirks — get_resources() returns None and populates res_df, and Vitis's own top-row total can disagree with its AreaEstimates total by a couple of LUTs."
---

# Reading the report — `CsynthParser`

Vitis writes `csynth.xml` into a solution directory at the end of C-synthesis.
[`CsynthParser`](../build/vitis.md) reads it.

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
p.total_resources       # {'BRAM_18K': 2, 'DSP': 64, 'FF': 14439, 'LUT': 10779, 'URAM': 0}
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
mem_r_stream_framed_task_32_Pipeline_A2S:A2S              1              3          <NA>          <NA>        <NA>        <NA>
fir_compute_unroll_task_32_Pipeline_FIRV:FIRV            1              9             0          2048           2        2055
fir_compute_unroll_task_32_Pipeline_LOAD:LOAD            1              2             0            32           0          32
```

The index is `<rtl entity>:<C++ loop label>`, so a loop is traceable back to the source label that
named it. This is a **timing** table living in the area report, and it is the quickest check that
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
entry_proc                                 0    0       2     29     0
fir_cmd_rx_task_32_s                       0    0      74    132     0
mem_r_stream_framed_task_32_s              0    0     472    833     0
fir_compute_unroll_task_32_s               0   64   10480   5980     0
mem_w_stream_framed_done_task_32_8_s       0    0    1464   1850     0
fir_block                                  2   64   14439  10781     0
Total                                      2   64   14439  10779     0
Available                                280  220  106400  53200     0
```

One tidy table of just the counters, with `Total` and `Available` appended as rows. Convenient for
eyeballing; note it still contains the nesting, so it is for reading, not for summing.

{: .note }
> **Two quirks worth knowing before they look like bugs.**
>
> `get_resources()` returns `None` and populates `p.res_df` as a side effect — assigning its return
> value gets you nothing.
>
> The `fir_block` row (10781 LUT) and the `Total` row (10779 LUT) **disagree by 2**, while FF, DSP and
> BRAM agree exactly. Vitis derives them slightly differently. Waveflow uses `AreaEstimates` (the
> `Total` row) as the design total, consistently, so figures elsewhere in these docs are on that basis.

## See also

- [FPGA resources](./xilinx.md) — what the counters mean and how far to trust them.
- [Composite kernels](./composite.md) — turning `module_info` into per-module numbers that add up.
