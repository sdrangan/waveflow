---
title: Playing a stored waveform
parent: Examples
nav_order: 9.6
has_children: true
audience: python
summary: "The worked example for RfShotTx: one transmitter, two command streams, and the same RTL answering both. A driver pushes in-band frames — a header then samples, TLAST at the end — the design loads them into a BRAM behind a lock, and a real Rfdc plays them out at the converter's grid. The finite stream plays three passes and goes quiet; the infinite one is preempted mid-play and switches waveform. Every verdict the protocol has is exercised across the two, and the playout is byte-identical between pysim and RTL."
---

# Playing a stored waveform

`examples/rf_shot_tx` is the worked example for [`RfShotTx`](../../guide/rf/rfshotbuf/tx.md) — the
transmit half of the finite sample buffer. A host hands it a waveform once; it holds it in a BRAM and
plays it at the converter's own rate, answering every command with exactly one verdict.

**This page is about the example.** What the design *is* — the protocol, the two play modes, the five
verdicts, the lock underneath — is [the guide's](../../guide/rf/rfshotbuf/), and this page links
rather than restates.

```
StreamDriver --[ShotTxHdr | dense words … TLAST]--> RfShotTx.s_in
RfShotTx.resp_out --> StreamSink            (one ShotTxResp per header)
RfShotTx.samp_out --> Rfdc.tx_streams[0] | Rfdc.tx_rf --RFSampIF--> RfDataSink
```

## What it plays

![The two playouts](./images/playout.svg)

The shaded runs are **filler** — the value the design writes when it has nothing to play. They are
not gaps: a converter comes due on a fixed grid, and a design that stopped writing would starve it.
Reading the figure top to bottom is reading the whole design:

* **Above, the finite path.** A `SHOT_LOAD` for three passes. The waveform repeats exactly three
  times and then the design **goes quiet** — it stopped on purpose rather than running out of
  stream. Everything arriving behind it is answered `SHOT_BUSY`.
* **Below, the infinite path.** A `SHOT_LOOP` starts playing; a second load arrives mid-play and
  **preempts** it. The gap between the two waveforms is the handover — how long the converter plays
  filler while the memory changes hands, because this design holds
  [one region](../../guide/rf/rfshotbuf/tx_internal.md#finding-tx-holds-one-region-rx-holds-two).
  The long tail is a `SHOT_SHORT` load that is stored and then never played.

The figure is rendered from the **pysim** run and regenerates with no toolchain; it is a picture of
the RTL because `test_both_backends_agree_sample_for_sample` asserts the two are byte-identical.

## Two scenarios, and they cannot be one

The example drives **one design** with **two command bundles** — `vectors/cmd` and
`vectors/cmd_loop` — through two hand-written XSI mains that differ only in three bundle names. That
the RTL is the same in both is the claim; a second testbench *graph* would be a second model of one
design.

They cannot be a single stream, and the reason is the design rather than the harness. A file-driven
driver never reads a verdict, so a stream that opens with a **finite** shot has every later frame
answered `SHOT_BUSY` — that *is* what `SHOT_BUSY` is — and a stream that opens with an **infinite**
one can never demonstrate a refusal. One scenario per opcode is the minimum.

| | `vectors/cmd` | `vectors/cmd_loop` |
|---|---|---|
| opens with | `SHOT_LOAD`, `nrepeat = 3` | `SHOT_LOOP` |
| verdicts produced | `LOADED`, `BUSY`, `WRONG_LEN`, `ZERO_LEN`, `END`→`LOADED` | `LOADED`, `WRONG_LEN`, `ZERO_LEN`, `LOADED`, `SHORT`, `END`→`LOADED` |
| what it proves | a finite shot is not truncated | a load mid-play preempts, and a short shot never plays |

Between them every legal verdict is exercised — asserted by
`test_all_five_verdicts_and_the_fence_appear_across_the_two_streams`.

## The geometry

| | value | what it is |
|---|---|---|
| `depth` | 256 words | the memory |
| `nword` | 64 words | one shot — 256 samples at 4 samples/word |
| `base` | 192 | the region, at the **top** of the memory |
| `blk_words` | 16 | words per chunk: the lock poll period |
| `samp_rate` | 256 MSa/s | the converter's grid |

**`base` is non-zero on purpose.** `base + offset` is the shape of the byte-versus-word addressing
bug that had every BRAM design in this repo mis-addressed while a smaller example stayed green, so
the region sits at the very top where the arithmetic is exercised.
`test_the_write_addresses_reach_the_last_element_and_no_further` asserts the writer touches exactly
`192..255`.

## Pages

- [**Running it**](./run.md) — the build rungs, and what each produces.
- [**Taking it to RTL**](./rtl.md) — the RTL run and every measured number, with the gate that
  asserts it.

## Related

- [`RfShotTx`](../../guide/rf/rfshotbuf/tx.md) — the design: ports, messages, verdicts, the rules.
- [Internals](../../guide/rf/rfshotbuf/tx_internal.md) — the tasks, the lock protocol, the findings.
- [`examples/rf_shot_rx`](../rf_shot_rx/) — the receive half of the same family.
