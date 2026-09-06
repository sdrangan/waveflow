---
title: Running it
parent: Playing a stored waveform
grand_parent: Examples
nav_order: 1
audience: python
summary: "The build rungs for rf_shot_tx and what each produces: the SimPy golden that checks both scenarios, the lowering to an ap_ctrl_none top plus its memory and wrapper, the XSI harness and both command bundles, Vitis C-synthesis, and the two on-demand figure rungs that need no toolchain."
---

# Running it

`rf_shot_tx_build.py` is a `BuildDag`, and the rungs are ordered so a failure is cheapest to
diagnose at the rung that caused it.

```bash
cd examples/rf_shot_tx
python rf_shot_tx_build.py                       # default: --through csynth
python rf_shot_tx_build.py --through pysim       # no toolchain needed
python rf_shot_tx_build.py --through sync_docs_figures   # the committed figure
```

| rung | what it does | needs |
|---|---|---|
| `pysim` | runs **both** scenarios in SimPy and checks the verdicts and the playout | — |
| `codegen_dut` | the `ap_ctrl_none` top, its tcl, its port map, the memory beside it, the **wrapper** that joins them, and the `$dumpvars` second top | — |
| `codegen_tb` | the XSI harness + main, and **both** scenario bundles | — |
| `csynth` | Vitis HLS C-synthesis; re-emits `rtl_<wrapper>.f` from the RTL on disk | Vitis HLS |
| `playout_figure` | renders `results/playout.svg` from a pysim run | — |
| `sync_docs_figures` | promotes it into `docs/examples/rf_shot_tx/images/` with a provenance record | — |

**What a simulator elaborates is the wrapper**, `rf_shot_tx_top` — the kernel plus its hand-written
`bram_t2p` memory — not the kernel. The kernel has `buf_w` / `buf_r` ports; the wrapper joins them to
the memory instance beside it, so a testbench sees only AXI-Stream.

## The pysim rung is the golden, and it checks both streams

`pysim` writes `results/rf_shot_tx_pysim.json` and asserts, per scenario, that every header was
answered with the right verdict in the right order and that the playout has the right shape. It needs
no toolchain, so it is the rung to run while changing the design.

The same functions the rung calls are what the RTL gate compares against — `check_responses`,
`check_finite_playout`, `check_loop_playout` — so the two backends are checked by one set of
statements rather than two.

## `config_rtl -reset state`, and why it is here and nowhere else

`SOLUTION_CONFIG` carries it, and it is not boilerplate. The player holds four `static`s and **writes
before it reads** — writing without being asked is what *the side that cannot stop* means — and an
`hls::task` in that shape advances during reset. Every static carries `#pragma HLS reset` *and* the
solution needs `config_rtl -reset state`, which is what actually closed it under Vitis 2025.1. The
loader opens with a blocking read and inherits none of this.

## The figure rungs need no toolchain, deliberately

`playout_figure` renders from **pysim**, not from a VCD. A VCD-sourced figure needs Vivado to
re-render, so in practice it is re-rendered rarely and goes stale quietly — which is what happened to
an earlier hand-drawn figure in this area and why
[`plans/rf_shot_unify.md`](../../guide/rf/rfshotbuf/) Stage C declined to add one it could not gate.

Two things make this one safe to commit:

* it regenerates anywhere, with one command and no license;
* its equality with the RTL is **already a gate** —
  `test_both_backends_agree_sample_for_sample` asserts the pysim playout is byte-identical to the RTL
  one, so a figure drawn from pysim is a figure of the RTL and something fails if that stops being
  true.

`sync_docs_figures` writes `images/sync_status.json` beside the committed SVG — source path plus
content hash — so staleness is detectable without re-rendering. The SVG itself is deterministic
(`metadata={"Date": None}` and a fixed `svg.hashsalt`), so a re-render that changes nothing produces
no diff.

## Next

- [Taking it to RTL](./rtl.md) — the XSI run and every measured number.
- [The design](../../guide/rf/rfshotbuf/tx.md) — what the ports and the protocol are.
