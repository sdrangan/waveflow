---
title: Systolic engine
parent: Systolic Matrix Multiply
grand_parent: DSP Blocks
nav_order: 3
audience: python
api: [SystolicEngine, SystolicEngineMem]
summary: "SystolicEngine, the redesign of SystolicUnit: operands loaded straight into banked stream-of-blocks buffers the array reads in place (no copy), a streaming systolic array whose tiles follow every max(kp, R·C/L, L) cycles, exact accumulation over k-panels with one rounding, keep_a reuse, and the default command-response shape. SystolicEngineMem puts it behind memory: the output-stationary outer loop for matrices larger than the engine, over block-major memory, fed by its own burst reader. Measured at RTL (Vitis 2025.1, centre): 175 cycles per 8×8×32 job against SystolicUnit's 390 -- bound by its input, the core takes 87 -- and a 32×32×64 product through memory exact at 6,470 cycles."
---

# Systolic engine

`SystolicEngine` and `SystolicEngineMem` (`waveflow/linalg/systolic_engine.py`,
`systolic_engine_mem.py`) are the redesign of the [systolic unit](index.md), from what its
[architecture](architecture.md) and [timing](timing.md) showed. The arithmetic is the same -- complex
fixed point, exact sums, one rounding, bit-exact with `waveflow.linalg.matmul.matmul` -- and so are the
traits and formats. What changes is how the operands reach the array and how the array runs.

All numbers here: Vitis HLS / Vivado xsim **2025.1**, `xczu48dr-ffvg1517-2-e`, 4 ns, the centre
configuration (`Mmax 8, Kmax 8, Nmax 32, Pmax 4, R 4, C 8, L 4`, 12-bit operands, 64-bit words).

## What changed, and what it bought

| | `SystolicUnit` | `SystolicEngine` |
|---|---|---|
| operands into the array | copied from the block buffers into the core's banks (90 cycles a job) | loaded straight into **banked** buffers the array reads in place: no copy |
| a tile | sweep, then drain, then the next tile: 36 cycles | **streaming**: each element latches its sum on a `first` flag and starts the next tile at once: `P = max(kp, R·C/L, L)` = 8 cycles |
| `K` larger than the buffers | not possible | `npanels` k-panels accumulated exactly on chip, rounded once |
| reuse of `A` | `nb` (core only) | `keep_a`: one `A` on the wire for `nb` outputs |
| protocol | status in the reply header, errors drained | a `Resp` per output, a `Status` per command on its own stream, errors fatal |
| `8×8×32` back to back, stream-fed | 390 cycles a job | **175** (input-bound: 162 words a job; the core takes 87) |
| unit resources (csynth) | 136 DSP, 20.5k LUT, 18 BRAM18K | 154 DSP, 37.0k LUT, 26 BRAM18K |

## The engine

```mermaid
flowchart LR
    sin["s_in: commands + operands"] --> RX["EngineRx"]
    RX -->|"A words"| LA["EngineLoadA"]
    RX -->|"B words"| LB["EngineLoadB"]
    RX -->|"job"| CORE["EngineCore"]
    RX -->|"reply"| ST["EngineStore"]
    LA -->|"X: a bank per row"| CORE
    LB -->|"B: a bank per array column"| CORE
    CORE -->|"C"| ST
    ST --> sout["s_out: Resp + C"]
    ST --> mst["m_status: Status"]
```

**The command** (`EngineCmd`, one burst on `s_in`): `tx_id`, `op` (`MUL` or `MUL_AH`), `m`, `kp`, `n`,
`npanels`, `nb`, `keep_a`. The operands follow, one burst each, in the order the core takes them: with
`keep_a`, `A` then the `nb` matrices `B`; otherwise, per output and per panel, `A` then `B`. `A` is
`m × kp` (`kp × m` for `MUL_AH`) and `B` is `kp × n`, row-major memory elements. Each output is
`C = q(Σ_p A_p·B_p)`, rounded once. Rules: `m` a multiple of `R`, `n` of `C`, `1 ≤ kp ≤ Kmax`,
`npanels ≤ Pmax`, `keep_a` only with one panel; otherwise `BAD_OP` / `BAD_DIMS`, and the receiver stops
taking input (fatal, until a reset).

**Two loaders.** The receiver sends `A` words to one loader and `B` words to the other, so each fills
its next block while the core sweeps the current one. One loader writing both in turn would serialize:
the RTL ping-pong lets a writer *commit* a block only after the reader frees the earlier one, and the
core frees `A` and `B` together. The `A` channel holds a whole `A`, so the receiver moves straight on to
`B` while the `A` loader -- a value a cycle -- drains it.

**The banked buffers.** `X` is `[Mmax][Kmax]`, a bank per row; `B` is `[Kmax][Nmax/C][C/LW][LW]`, a
bank per array column, laid out so a word's `LW` values land in lanes the compiler can tell apart
(`LW` values per word, a word a cycle). They are declared on the channels with
`StreamOfBlocksIF.bank(shape, partition)`, which the generated top turns into a partitioned
`stream_of_blocks`: Vitis builds a ping-pong per bank, and a reader takes one value from every bank a
cycle ([witness](../../../../plans/witness/systolic_stream/README.md)).

**The streaming array.** One pipelined loop runs every tile of a panel: row `i` takes tile `q`'s value
`kk` at step `q·P + kk + i`, column `j` at `q·P + kk + j`, zero for `kk ≥ kp`; a `first` flag and the
tile's index ride with the row's values. On the flag an element latches its sum into a shadow and
starts the next tile -- from zero on the first panel, from its partial sum on the others (an
accumulator store in LUTRAM). Shadows drain while the next tile runs, group `(i, cg)` at step
`(q+1)·P + L + n` in diagonal order -- into the store, or on the last panel rounded once into `C`.
Every loop of the engine is at II 1; the core is one copy of the array (137 DSP).

## Behind memory: the outer loop

```mermaid
flowchart LR
    job["s_job"] --> SEQ["EngineSeq"]
    SEQ -->|"feed: a tile's panels"| FD["EngineFeeder (m_axi read)"]
    FD -->|"EngineCmd, A, B, A, B ..."| ENG["SystolicEngine"]
    SEQ -->|"tile"| WR["EngineWriter"]
    ENG --> WR
    WR -->|"MemWCmd | C"| MW["MemWStream"]
    MW --> done["s_done"]
```

`SystolicEngineMem` takes one `EngineJobDesc` per product (`tx_id`, `op`, `M`, `K`, `N`, and the base
word addresses of `A`, `B` and `C`) and answers each with an `EngineDone`. The loop is output-stationary:
`C` is covered in `Mmax × Nmax` tiles, each one engine command of `npanels = K / Kmax` panels. The
sequencer sends the **feeder** -- the read port's owner -- one descriptor per tile, and the feeder streams
the tile in one firing: the `EngineCmd`, then `A(i,0) B(0,j) A(i,1) B(1,j) ...`, a burst loop per operand
panel, a word a cycle. The writer turns each tile's `Resp | C` into `MemWCmd(C(i,j)) | C`, echoing the
job's `EngineDone` on its last tile. The `m_axi` reader and writer of one generated top must fire equally
often (their pointer FIFOs couple them); the feeder fires once per tile and the writer writes once per
tile, so they balance.

**Why a feeder of its own** (each measured on the `32×32×64` product, its read port traced):

| Reader | Done at | Read port busy |
|---|---|---|
| the framework's in-band `MemRStream`, a firing per read command | 6,655 | 80%: ~16 idle cycles at each of 8 commands a tile |
| one flattened loop with a computed address | 10,639 | 49%: no burst inferred, a single-beat read every two cycles |
| a tile's `A` panels and `B` panels as two runs | 7,984 | 67%: `B` waits behind the core's last panel (one block of buffering) |
| **a burst loop per panel, in the engine's order** | **6,470** | 82%: ~12 idle cycles at each switch between burst loops |

**Memory is block-major** (`block_major`): `A`'s panel `(i, p)` at `a_base + (i·P + p)·wa`, `B`'s
`(p, j)` at `b_base + (p·NT + j)·wb`, `C`'s tile `(i, j)` at `c_base + (i·NT + j)·wc`, each one burst.
`M`, `K`, `N` must be multiples of `Mmax`, `Kmax`, `Nmax` and `K ≤ Pmax·Kmax`; pad with zeros.

## Measured

| What | Result |
|---|---|
| engine, six commands (panels, `keep_a`, `Aᴴ` with panels, `kp = 1`) | bit-exact at RTL, pysim equal |
| engine, 4 × `8×8×32` back to back, stream-fed | **175 cycles a job**: 162 words in, the receiver's 13 cycles of command handling |
| wrapper: a 2×2-tile 2-panel job, an `Aᴴ` job of 4 panels, a single tile | `C` in memory exact at RTL |
| wrapper: `32×32×64` (8 tiles × 4 panels) | exact at RTL, done at **cycle 6,470** against an input floor of about 5,136 (~79%) |
| the core's own time, `8×8×32` and `8×1×32`, `8×2×32` (from the trace: its first touch of `X` to its commit of `C`) | **87 cycles** whatever `kp` -- never the bound at 64-bit words: the input binds at `kp = 8` (175), the output at short `kp` (141: `C` is 128 words whatever `kp`) |
| engine resources | 154 DSP, 37.0k LUT, 25.6k FF, 26 BRAM18K, est. 3.05 ns |
| wrapper resources | 157 DSP, 42.9k LUT, 31.1k FF, 30 BRAM18K, est. 3.35 ns |

Gates: `tests/linalg/test_systolic_engine.py` (`-m xsi` for the RTL ones).

## Limits

- **Not calibrated.** The core's pysim time is one phase per panel (`b_blk.first -> b_blk.last`,
  `20 + tiles·P`) seeded from the prototype; the calibration is the last step of the plan, once the
  design settles.
- **`R ≤ L`.** The drain schedule needs distinct diagonals; `R > L` is refused at construction.
- **Wrapper tiles are the maxima.** Every tile is `Mmax × Nmax × Kmax` per panel; other shapes pad.
- **The wrapper is not yet input-bound** (79% of the floor at `32×32×64`): each switch between the
  feeder's burst loops leaves the read port idle ~12 cycles, 8 switches a tile. Removing it needs bursts
  that overlap across loops (`hls::burst_maxi`, which the generated top does not yet declare) or deeper
  buffering of `B`, so `A` and `B` can each be read in one run.
- **I/O-bound at 64-bit words.** The core (87 cycles) has room: 128-bit words would bring the input to
  ~95 and the output to ~65 (the linalg word-width guard, `lanes.WORD_BITS_SUPPORTED`, allows 32 and 64).
- **LUTs.** The array costs about 1.8× `SystolicUnit`'s LUTs for 2.2× its throughput; the per-element
  selects are the next place to look.
- **Ordered, not addressed.** Operands are reused in order (`keep_a`), not by address. Addressed
  regions would need hand-written T2P BRAMs shared between tasks (`T2pBram`, `LockedT2pMemIF`).
