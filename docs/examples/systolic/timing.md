---
title: Systolic timing results
parent: A systolic matrix multiply, measured
nav_order: 2
summary: "What the RTL measured (Vitis 2025.1, centre): the engine at 175 cycles per 8×8×32 product with 64-bit words (the input bound) and 141 with short panels (the output bound); with 128-bit words 94 whatever the panel -- the core's own floor, 64 tile steps and about 30 cycles a command, tied with the input. How the binding stage was found from block hold times and input gaps; the design's steps from 390 cycles to 94; the wrapper at 6,468 and 3,680 cycles on 32×32×64, bound by its read port, and why its feeder reads as it does; pysim on seeded phases against the RTL."
---

# Timing

All numbers are RTL, XSI, `xczu48dr-ffvg1517-2-e` at 4 ns, Vitis HLS / Vivado xsim **2025.1**, the
centre configuration, every output checked against the model -- from
`examples/systolic/measured.json`, written by `systolic_measure` ([the harnesses](test.md)).

## The engine against its bounds

[The theory](../../guide/dsp/systolic/systolic_theory.md#three-bounds-on-a-panel) bounds a product by its
compute (`T·P` steps), its input (`kp·(m + n)` values) and its output (`m·n` values), at one word a
cycle. Products back to back, the interval between consecutive outputs:

| Workload | Word | Compute | Input (words) | Output (words) | Measured | What binds |
|---|---|---|---|---|---|---|
| `steady`: `8×8×32` | 64-bit | 64 | 160 + 2 | 128 + 1 | **175** | the input |
| `short`: `8×1×32`, `8×2×32` | 64-bit | 64 | 20, 40 + 2 | 128 + 1 | **141** | the output |
| `steady`: `8×8×32` | 128-bit | 64 | 80 + 1 | 64 + 1 | **94** | the core and the input, tied |
| `short`: `8×1×32`, `8×2×32` | 128-bit | 64 | 10, 20 + 1 | 64 + 1 | **94** | the core |

(`+ 1`, `+ 2`: the command or the `Resp` header.) The gaps between a bound and the measurement are
fixed costs per command:

- **The receiver's ~13 cycles a command** -- reading and checking the command, sending the jobs --
  during which `s_in` stalls: 162 words take 175 cycles at 64 bits, 81 take ~94 at 128.
- **The core's ~30 cycles a command beyond its 64 tile steps**: the empty tile that runs the last tile
  out (`L + R·C/L = 12` steps) and ~18 of restart -- the command's read, the collector's job, the block
  locks, the loop's pipeline fill. The short workload shows it alone: at 128 bits nothing else is near
  94 cycles.

So at 128-bit words the engine is at its core's floor and its input's at once: making either faster
alone gains nothing; both have to shrink.

## Finding the stage that binds

The measurement says how long; a trace says why. With `--trace` the harness dumps the top's channels,
and `waveflow.utils.trace.load_trace` reads them by name: each stream-of-blocks block's acquire and
release (`block_events`), each FIFO beat, and the input's accepted beats (`vectors/s_in_acc`). Before
the array ran continuously, the same 128-bit `steady` run read:

| Channel | Seen | Says |
|---|---|---|
| `C` block, the core's side | held 83 cycles, then ~20 idle before the next, every 103 | the core takes 103 a product |
| `s_in` accepted beats | 81 words in ~94 cycles, gaps of 2, 8, 6 a command | the input could go faster |

-- core-bound. Running the array continuously across a command's panels and outputs (each panel had
paid its last tile's fill and drain, 20 steps) took it to 94, where the input meets it.

## The design, step by step

The same `8×8×32` products back to back, through each design change:

| Design | 64-bit | 128-bit | What changed |
|---|---|---|---|
| the earlier `SystolicUnit` | 390 | -- | operands copied into the array's banks; one tile at a time |
| the engine as first built | 175 | 103 | banked buffers read in place; tiles overlapping (`P` steps each) |
| the engine, continuous | **175** | **94** | the array's state across a command's panels; the drain to a collector |

At 64 bits the input has bound since the first engine; the changes since have bought the 128-bit case
and the multi-panel commands.

## The wrapper

`SystolicEngineMem` on `32×32×64` -- 8 tiles of 4 panels -- `C` exact in memory:

| Word | Done at | Its input (each tile's panels, a word a cycle) | Engine time (8 tiles × 4 panels) |
|---|---|---|---|
| 64-bit | **6,468** | ~5,136 (79%) | ~2,300 (~290 a tile) |
| 128-bit | **3,680** | ~2,570 (70%) | ~2,300 (~290 a tile) |

**The read port binds.** A tile takes ~460 cycles at 128 bits; its 321 words and the engine's ~290
cycles both fit inside. The rest is the read port standing idle: the feeder reads each panel with a
burst loop of its own, and each switch between loops costs ~12 cycles, 8 a tile. Running the engine's
array continuously moved the product only from 3,693 to 3,680 cycles (6,470 to 6,468 at 64 bits).

**Why the feeder reads that way.** Each reader was measured on this product with 64-bit words, its read
port traced:

| Reader | Done at | Read port busy |
|---|---|---|
| the framework's in-band `MemRStream`, a firing per read command | 6,655 | 80%: ~16 idle cycles at each of 8 commands a tile |
| one flattened loop with a computed address | 10,639 | 49%: no burst inferred, a single-beat read every two cycles |
| a tile's `A` panels and `B` panels as two runs | 7,984 | 67%: `B` waits behind the core's last panel (one block of buffering) |
| **a burst loop per panel, in the engine's order** | **6,470** | 82%: ~12 idle cycles at each switch between loops |

Closing the gap needs bursts that overlap across loops (`hls::burst_maxi`) or a deeper `B` buffer, so
each tile reads `A` and `B` in one run each.

## pysim against the RTL

pysim runs the same scenarios on the core's **seeded** `panel` phase (its tile steps plus a guessed
intercept), not a calibrated one. Its run ends 6% to 80% after the RTL's last output -- 1.44× on the
64-bit `steady` run, 1.47× at 128, 1.74× on the 64-bit wrapper: the receiver's, loaders' and feeder's
own times are not yet phases at all. The calibration fits them at RTL
(`plans/systolic_characterization.md`); this page reports measurements until then.
