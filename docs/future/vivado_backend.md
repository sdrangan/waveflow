---
title: Vivado IPI Backend
parent: Future
---

# Vivado IPI Backend

> **Status:** Not implemented. This page describes intended future work.

## Concept

A Vivado IPI backend would extend Waveflow outputs beyond HLS kernel code into block-design integration flows. The intended capability is to map component interfaces and generated artifacts into reproducible IPI assembly steps so users can build larger systems without manual block-diagram wiring.

## Status

Current code generation and examples target HLS-centric flows. There is no implemented backend for IPI packaging, block automation scripts, or end-to-end export into Vivado block designs.

## See also

- [plans/board_packaging.md](https://github.com/sdrangan/waveflow/blob/main/plans/board_packaging.md) — the staged plan: each example exported as Vitis IP, wired in a Vivado block design,
  built to a bitstream, and driven from PYNQ on an RFSoC 4x2.
- [plans/rfsoc_4x2_bringup.md](https://github.com/sdrangan/waveflow/blob/main/plans/rfsoc_4x2_bringup.md) — the board, and the archival contract for a reproducible reference design.
