---
title: C-Synth Resource Estimation
parent: Streaming polynomial
nav_order: 8
summary: "What synthesis built at each width, and whether every loop reaches II = 1: C synthesis of the poly and poly_bw64 tops, the report parsed into a per-loop pipeline table and a resource summary, and a build that fails if any loop's initiation interval is above 1. Doubling the word width doubles the DSPs, because two samples are evaluated per cycle."
---

# C synthesis

What did synthesis build, and does every loop reach II = 1?  C simulation says the kernel computes
the right answer.  Synthesis says what hardware computes it, and whether it can take a sample word
every clock cycle.

| Step | Produces | What it does |
|------|----------|--------------|
| `csynth_w32`, `csynth_w64` | `report_dir_w*` | Runs `run.tcl` with `WAVEFLOW_POLY_STAGE=synth` and that width: C synthesis, then RTL co-simulation of the timing scenario (see the next page), in `waveflow_poly_w32_proj/` or `waveflow_poly_w64_proj/` |
| `inspect_synth_w32`, `_w64` | `loop_df_w*`, `res_df_w*` | Parses `csynth.xml` with `waveflow.utils.csynthparse.CsynthParser`, prints the loop and resource tables, and **fails the build** if any loop has `PipelineII > 1` |

## The loops

At both widths the sample loop -- the lane loop in `transaction()`, one input word per iteration --
pipelines at **II = 1**, with a depth of 29 cycles: the float32 multiply-add chain of Horner's rule.

| Loop | II | Depth | What it is |
|---|---|---|---|
| sample loop (`poly_body_impl.tpp`, `for (int i = 0; i < nsamp; i += pf)`) | 1 | 29 | one sample word in, one result word out, per cycle |
| coefficient unpack (generated `poly_cmd_hdr.h`, reading the header) | 1 | 1 | the header's coefficients, a word per cycle |
| command loop (`while (true)` in `body`) | -- | -- | not pipelined: one command at a time |

A `PipelineII > 1` on any loop fails the build.  II discipline is worth catching with a build step
rather than leaving it buried in a synthesis log.

## The resources

On the `xc7z020` at 100 MHz:

| Top | Samples per word | DSP | FF | LUT | BRAM |
|---|---|---|---|---|---|
| `poly` (32-bit words) | 1 | 15 | 2273 | 3030 | 0 |
| `poly_bw64` (64-bit words) | 2 | 30 | 3986 | 5708 | 0 |

The 64-bit top evaluates two samples per cycle, so it has two copies of the Horner datapath and
twice the DSPs.  It is also twice as fast on the sample burst (next page): width buys throughput
with area.

There is no BRAM, because the kernel stores nothing.  Each command's coefficients live in registers
for the length of that command, which is what rule 4 ("nothing carries over") looks like in
hardware.

## Run just this group

```bash
python examples/stream_inband/poly_build.py --through inspect_synth_w32
python examples/stream_inband/poly_build.py --through inspect_synth_w64
```

Produces `waveflow_poly_w*_proj/solution1/syn/report/csynth.xml`, `results/loop_df_w*.csv`,
`results/res_df_w*.csv`, and the tables on stdout.

## Check your understanding

1. Why does the 64-bit kernel use twice the DSPs of the 32-bit one?
2. What would `PipelineII = 2` on the sample loop mean for the throughput of each width?
3. The kernel has no BRAM.  Which rule of the contract makes that possible?

---

Next: [RTL co-simulation timing →](./05_cosim_timing.md)
