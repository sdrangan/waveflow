---
title: Systolic timing results
parent: A systolic matrix multiply, measured
nav_order: 2
summary: "What the RTL measured (Vitis 2025.1, centre): the engine at 171 cycles per 8×8×32 product with 64-bit words (the input bound) and 141 with short panels (the output bound); with 128-bit words 94 whatever the panel -- the core's own floor, 64 tile steps and about 30 cycles a command -- and 81 an output when a command carries four, at the input's bound. How the binding stage was found from block hold times and input gaps; the design's steps from 390 cycles to 94; the wrapper at 5,813 and 3,033 cycles on 32×32×64 once its feeder requests a tile's panels ahead; the core's panel phase calibrated at RTL, 5 + 1.00·steps cycles, exact on held-out panels; pysim within 5 to 12% of the RTL."
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
| `steady`: `8×8×32` | 64-bit | 64 | 160 + 2 | 128 + 1 | **171** | the input |
| `short`: `8×1×32`, `8×2×32` | 64-bit | 64 | 20, 40 + 2 | 128 + 1 | **141** | the output |
| `steady`: `8×8×32` | 128-bit | 64 | 80 + 1 | 64 + 1 | **94** | the core and the input, tied |
| `short`: `8×1×32`, `8×2×32` | 128-bit | 64 | 10, 20 + 1 | 64 + 1 | **94** | the core |

(`+ 1`, `+ 2`: the command or the `Resp` header.) The gaps between a bound and the measurement are
fixed costs per command:

- **The receiver's ~9 cycles a command** -- reading and checking the command, sending the jobs --
  during which `s_in` stalls: 162 words take 171 cycles at 64 bits.
- **The core's ~30 cycles a command beyond its 64 tile steps**: the empty tile that runs the last tile
  out (`L + R·C/L = 12` steps) and ~18 of restart -- the command's read, the collector's job, the block
  locks, the loop's pipeline fill. The short workload shows it alone: at 128 bits nothing else is near
  94 cycles.

**A command's fixed costs spread over its outputs.** With four outputs a command (`batch`, each its own
`A` and `B`), the outputs come **81** cycles apart on average at 128-bit words -- the input's 80 words
an output -- and about 156 at 64-bit (the input's 160; a run of eight outputs is too short to settle
exactly). The array's state cannot be carried from one command to the next -- both ways to try it, as
`static` registers and as a loop over commands in one firing, made csynth run out of memory (6.7 and
7.0 GB, against 0.4) -- so batching outputs into a command is how to reach the input's bound.

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
paid its last tile's fill and drain, 20 steps) took it to 94, where the input meets it. The `short`
workload then showed the 94 was also the core's own floor.

## The design, step by step

The same `8×8×32` products back to back, through each design change:

| Design | 64-bit | 128-bit | What changed |
|---|---|---|---|
| the earlier `SystolicUnit` | 390 | -- | operands copied into the array's banks; one tile at a time |
| the engine as first built | 175 | 103 | banked buffers read in place; tiles overlapping (`P` steps each) |
| the engine, continuous | 175 | 94 | the array's state across a command's panels; the drain to a collector |
| the receiver, one loop an output | **171** | **94** | no loop exit and re-entry between a command's `A` and `B` bursts |

At 64 bits the input has bound since the first engine; the changes since have bought the 128-bit case,
the multi-panel commands, and four cycles of the receiver's. Because the input binds, half the array does
as well: the engine with `R = 2` (16 elements, 91 DSP) runs the same products at 171 cycles too
([resources](resource.md#across-configurations)).

## The wrapper

`SystolicEngineMem` on `32×32×64` -- 8 tiles of 4 panels -- `C` exact in memory:

| Word | Done at | Its input (each tile's panels, a word a cycle) | Engine time (8 tiles × 4 panels) |
|---|---|---|---|
| 64-bit | **5,813** | ~5,136 (88%) | ~2,300 (~290 a tile) |
| 128-bit | **3,033** | ~2,570 (85%) | ~2,300 (~290 a tile) |

**The read port binds.** A tile takes ~380 cycles at 128 bits; its 321 words fit inside, and the engine's
~290 cycles too. The feeder requests a tile's eight panels ahead of their data (`hls::burst_maxi`) and
reads their words in one loop, so the requests queue in the `m_axi` adapter and the port no longer waits
between panels; what remains is the adapter's own gaps between bursts and the feeder's per-tile command.

**Why the feeder reads that way.** Each reader was measured on this product with 64-bit words, its read
port traced:

| Reader | Done at | Read port busy |
|---|---|---|
| the framework's in-band `MemRStream`, a firing per read command | 6,655 | 80%: ~16 idle cycles at each of 8 commands a tile |
| one flattened loop with a computed address | 10,639 | 49%: no burst inferred, a single-beat read every two cycles |
| a tile's `A` panels and `B` panels as two runs | 7,984 | 67%: `B` waits behind the core's last panel (one block of buffering) |
| a burst loop per panel, in the engine's order | 6,470 | 82%: ~12 idle cycles at each switch between loops |
| **every panel requested ahead (`hls::burst_maxi`), one read loop** | **5,813** | 88% of the input floor |

## The core's timed phase, calibrated

The core's `panel` phase -- from its `B` block's first access to its release -- is fitted at RTL by the
harness's calibration (`engine_harness --calibrate`): the `calib` workload, 18 commands with panels of
8 to 76 steps (every tile count, one and two panels, `Aᴴ`, `kp = 1`), traced, each panel's span read off
the waveform by name. At 64- and 128-bit words alike:

```
panel = 5 + 1.00 · steps       (cycles; steps = T·P, the gap, the run-out)
```

exact on all 24 fitted panels and on the 4 held out (36 and 60 steps): the step count the schedule
predicts is the RTL's to the cycle, plus 5. The records are filed by the core's identity in
`waveflow/linalg/calib/modules/engine_core-*`, one per word width, and pysim loads them.

## pysim against the RTL

With the calibrated phase -- and with the receiver, the feeder and the writer forwarding a word a cycle
as the RTL does -- pysim's runs end 5% to 12% before the RTL's last output (8% on the 128-bit `steady`
run, 5% on the 128-bit wrapper's `big` product; 23% on the mixed `test` workload). What pysim lacks are
the fixed costs per command outside the panels -- the receiver's command handling and the core's restart
between commands -- which are not yet phases of their own. Before, it ran 1.5× (the engine) to 1.9×
(the wrapper) long: its tasks stored and forwarded each burst, and the core sent `C` after its last panel
instead of draining it as the tiles ran.
