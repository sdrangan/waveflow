---
title: Writing a BFM Testbench
parent: Build System
nav_order: 7
---

# Writing a BFM testbench

At the XSI rung, the BFM testbench is the primary hand-authored artifact. It is the cycle-level analog of the single-kernel sequential C++ testbench used in Vitis C-sim: same golden intent, but now you must explicitly drive bus handshakes each cycle.

Reference implementation: [`examples/interleaver/xsi/interleaver_canon_bfm_tb.cpp`](https://github.com/sdrangan/waveflow/tree/main/examples/interleaver/xsi/interleaver_canon_bfm_tb.cpp).

## What the BFM is responsible for

1. Open the elaborated simulator DLL via XSI.
2. Resolve DUT ports by name.
3. Drive reset and clock.
4. Drive AXI-Stream command input (`TVALID/TDATA`) and consume done/output streams (`TREADY`/`TVALID`).
5. Emulate AXI-MM memory-side behavior for read and write channels.
6. Check DUT outputs against golden reference values.

## AXI-MM + AXI-Stream cycle-driving pattern

### AXI-Stream

- Keep held TB-side state (`cmd index`, `valid` flags, `done count`).
- A beat occurs only on `TVALID && TREADY`.
- Advance stream payload/index only after a successful beat.

### AXI-MM read channel model (AR/R)

- When DUT handshakes `AR`, capture base address/length.
- Enter an R-send state that returns memory words beat-by-beat.
- Assert `RLAST` on final beat.

### AXI-MM write channel model (AW/W/B)

- Handshake `AW` to capture write burst metadata.
- Accept `W` beats, applying `WSTRB` byte masks into the memory model.
- Emit `BVALID` response after final write beat (`WLAST`).

## Memory model behavior

In the interleaver BFM, `gmem0`/`gmem1` are backed by one flat word array. Address math converts AXI byte addresses to memory words (`addr / bytes_per_word`). Burst bookkeeping tracks beat counters and last-beat behavior.

For command/input/output handling, the BFM packs input commands into stream words, then unpacks output words from memory/streams and compares to expected golden values.

## Handshake and sampling discipline

The common pattern is:

1. Drive TB outputs.
2. Tick clock LOW and sample DUT outputs/ready-valid observations.
3. Tick clock HIGH.
4. Update TB state machines based on completed handshakes.
5. Drive next-cycle outputs.

Sampling in the clock-LOW phase keeps handshake accounting consistent and avoids off-by-one timing errors.

## Completion and throughput framing

Record per-job completion cycles (for example each done token/job completion), then derive:

- **fill latency** (first completion cycle),
- **steady-state period** (delta between successive completion cycles),
- and thus throughput framing for the pipeline.

This mirrors how single-kernel C++ TBs report correctness plus latency, but at explicit cycle granularity.

## Forward-looking automation target

A key future target is generating most BFM scaffolding from the component boundary port list (especially repetitive AXI channel plumbing). Today, `*_bfm_tb.cpp` remains the main hand-authored piece at this rung.

## See also

- [XSI Build Rung](./xsi.md) — terminology and full compile/elaborate/run flow.
