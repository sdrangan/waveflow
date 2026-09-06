---
title: Running it
parent: Capturing without losing anything
grand_parent: Examples
nav_order: 1
audience: python
summary: "The build rungs for rf_shot_rx and what each produces: the SimPy golden that checks the ramp is contiguous, the lowering to an ap_ctrl_none top plus its memory and wrapper, the XSI harness and the ramp bundle, and Vitis C-synthesis."
---

# Running it

```bash
cd examples/rf_shot_rx
python rf_shot_rx_build.py                      # default: --through csynth
python rf_shot_rx_build.py --through pysim      # no toolchain needed
```

| rung | what it does | needs |
|---|---|---|
| `pysim` | runs the capture in SimPy and checks the windows concatenate into a contiguous ramp | — |
| `codegen_dut` | the `ap_ctrl_none` top, its tcl, its port map, the memory, the **wrapper** joining them, and the `$dumpvars` second top | — |
| `codegen_tb` | the XSI harness + main, and the ramp bundle both backends drive | — |
| `csynth` | Vitis HLS C-synthesis; re-emits `rtl_<wrapper>.f` from the RTL on disk | Vitis HLS |

**What a simulator elaborates is the wrapper**, `rf_shot_rx_top` — the kernel plus its hand-written
`bram_t2p` memory. The kernel has `buf_w` / `buf_r` ports; the wrapper joins them to the memory
instance beside it, so the testbench sees only AXI-Stream.

## The reset trap is on the other side here

`RfShotTx`'s player **writes before it reads** and needs `config_rtl -reset state`. This design's
owner is the **capture**, and it *reads* before it writes — its first act is a blocking stream read,
so it stalls at reset like any requester. The statics still carry `#pragma HLS reset` because they
are state a reset should clear; what they do not need is the solution-level setting TX needs.

That asymmetry is worth knowing before copying one build's `SOLUTION_CONFIG` into the other.

## There is no second scenario

TX drives two command bundles because a file-driven driver cannot read a verdict, so one stream
cannot exercise both play modes. This design has **no command stream at all** — it captures
continuously and answers per window — so one run exercises everything it does.

## Next

- [Taking it to RTL](./rtl.md) — the XSI run and every measured number.
- [The design](../../guide/rf/rfshotbuf/rx.md) — the ports, the window header, the two rules.
