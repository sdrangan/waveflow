---
title: Systolic engine
parent: Systolic Matrix Multiply
grand_parent: DSP Blocks
nav_order: 3
audience: python
api: [SystolicEngine, SystolicEngineMem]
summary: "SystolicEngine, the redesign of SystolicUnit: operands loaded straight into banked stream-of-blocks buffers the array reads in place (no copy), a streaming systolic array whose tiles follow every max(kp, R·C/L, L) cycles, exact accumulation over k-panels with one rounding, keep_a reuse, and the default command-response shape. SystolicEngineMem puts it behind memory: the output-stationary outer loop for matrices larger than the engine, over block-major memory, fed by its own burst reader. Measured at RTL (Vitis 2025.1, centre): 175 cycles per 8×8×32 job with 64-bit words against SystolicUnit's 390, bound by the input; 103 with 128-bit words, bound by the core -- and a 32×32×64 product through memory exact at 6,470 cycles (64-bit) and 3,693 (128-bit)."
snippets: run
---

# Systolic engine

`SystolicEngine` and `SystolicEngineMem` (`waveflow/linalg/systolic_engine.py`,
`systolic_engine_mem.py`) are the redesign of the [systolic unit](index.md), from what its
[architecture](architecture.md) and [timing](timing.md) showed. The arithmetic is the same -- complex
fixed point, exact sums, one rounding, bit-exact with `waveflow.linalg.matmul.matmul` -- and so are the
traits and formats. What changes is how the operands reach the array and how the array runs.

All numbers here: Vitis HLS / Vivado xsim **2025.1**, `xczu48dr-ffvg1517-2-e`, 4 ns, the centre
configuration (`Mmax 8, Kmax 8, Nmax 32, Pmax 4, R 4, C 8, L 4`, 12-bit operands), with 64-bit words
unless a row says 128.

## What changed, and what it bought

| | `SystolicUnit` | `SystolicEngine` |
|---|---|---|
| operands into the array | copied from the block buffers into the core's banks (90 cycles a job) | loaded straight into **banked** buffers the array reads in place: no copy |
| a tile | sweep, then drain, then the next tile: 36 cycles | **streaming**: each element latches its sum on a `first` flag and starts the next tile at once: `P = max(kp, R·C/L, L)` = 8 cycles |
| `K` larger than the buffers | not possible | `npanels` k-panels accumulated exactly on chip, rounded once |
| reuse of `A` | `nb` (core only) | `keep_a`: one `A` on the wire for `nb` outputs |
| protocol | status in the reply header, errors drained | a `Resp` per output, a `Status` per command on its own stream, errors fatal |
| `8×8×32` back to back, stream-fed | 390 cycles a job | **175** (input-bound: 162 words a job); **103** with 128-bit words (core-bound) |
| unit resources (csynth) | 136 DSP, 20.5k LUT, 18 BRAM18K | 154 DSP, 37.0k LUT, 26 BRAM18K |

## Which one, and how large

Two modules, one array. Pick by where the operands live:

| | `SystolicEngine` | `SystolicEngineMem` |
|---|---|---|
| operands and results | on streams: a command, then `A` and `B` bursts in; a `Resp` and `C` out | in memory: one job descriptor in, `C` written back, an `EngineDone` out |
| `M` (rows of `C`) | up to `Mmax`, a multiple of `R` | any multiple of `Mmax` (tiled) |
| `N` (columns of `C`) | up to `Nmax`, a multiple of `C` | any multiple of `Nmax` (tiled) |
| `K` (the inner dimension) | up to `Pmax · Kmax`: `npanels` panels of `kp ≤ Kmax`, summed on chip | the same bound, `K` a multiple of `Kmax` |
| reuse | `keep_a`: one `A` for `nb` products | none (each tile reads its panels) |
| memory layout | -- | block-major panels and tiles (`block_major`) |

`SystolicEngineMem` *contains* a `SystolicEngine`: it adds the tile loop, a reader, a writer and the
memory ports. Neither lifts the bound on `K`: the partial sums stay on chip, so a longer `K` needs a
larger `Pmax`. Both check a request before running it and refuse what they cannot do, fatally (the
default command-response rule):

```python
from waveflow.linalg.systolic import MatmulOp
from waveflow.linalg.systolic_engine import SystolicEngine, command
from waveflow.linalg.systolic_engine_mem import EngineJobDesc, job_status
from waveflow.simulation.simulation import Simulation
from waveflow.utils.fixputils import Format, OMode, QMode


def reg(W, I):                     # W bits, I of them integer; rounded and saturated
    return Format(W, I, True, QMode.AP_RND, OMode.AP_SAT)


eng = SystolicEngine(name="systolic_engine", sim=Simulation(), Mmax=8, Kmax=8, Nmax=32, Pmax=4,
                     R=4, C=8, L=4, word_bits=128, a=reg(12, 3), b=reg(12, 4), c=reg(12, 5))
for c in (command(1, MatmulOp.MUL, 8, 8, 32, npanels=4),    # K = 32: four panels, summed on chip
          command(2, MatmulOp.MUL, 8, 8, 32, npanels=5),    # K = 40: more panels than Pmax
          command(3, MatmulOp.MUL, 16, 8, 32)):             # M = 16: more rows than Mmax
    print("engine", int(c.tx_id), eng.status(c).name)

for tx, (M, K, N) in enumerate([(64, 32, 128), (64, 40, 128)], start=4):
    d = EngineJobDesc()
    d.tx_id, d.op, d.M, d.K, d.N = tx, MatmulOp.MUL, M, K, N
    print("memory", tx, job_status(d, Mmax=8, Kmax=8, Nmax=32, Pmax=4).name)
```

```text
engine 1 OK
engine 2 BAD_DIMS
engine 3 BAD_DIMS
memory 4 OK
memory 5 BAD_DIMS
```

**The word width** (`word_bits`, 32, 64 or 128) sets how many elements a stream or memory word carries:
two 32-bit memory elements at 64 bits, four at 128. The engine's command and status ports stay 64
bits (`endpoint_port_widths`); the data ports and the wrapper's memory take the word width.

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
| engine, 128-bit words: the six commands, then 4 × `8×8×32` | bit-exact at RTL; **103 cycles a job**, now **core-bound**: the core holds each `C` block 83 cycles (64 tile steps, its fill and drain) and waits ~20 between jobs; the 81 input words take ~94 cycles, the 65 output words 65 |
| wrapper, 128-bit words: the three test jobs and `32×32×64` | exact at RTL; done at **cycle 3,693** (~460 a tile) against an input floor of about 2,570; which stage binds is not yet traced |
| the core's own time, `8×8×32` and `8×1×32`, `8×2×32` (from the trace: its first touch of `X` to its commit of `C`) | **87 cycles** whatever `kp` -- never the bound at 64-bit words: the input binds at `kp = 8` (175), the output at short `kp` (141: `C` is 128 words whatever `kp`) |
| engine resources | 154 DSP, 37.0k LUT, 25.6k FF, 26 BRAM18K, est. 3.05 ns |
| wrapper resources | 157 DSP, 42.9k LUT, 31.1k FF, 30 BRAM18K, est. 3.35 ns |
| engine resources, 128-bit words | 154 DSP, 36.9k LUT, 25.7k FF, 30 BRAM18K, est. 3.05 ns |
| wrapper resources, 128-bit words | 157 DSP, 43.1k LUT, 33.2k FF, 38 BRAM18K, est. 3.35 ns |

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
- **Core-bound at 128-bit words.** A job takes 103 cycles: the core's 64 tile steps plus ~39 of fill,
  drain and restart between jobs, while the input needs ~94. Wider words no longer pay; overlapping one
  panel's drain with the next panel's fill is the next gain, and then the receiver's ~13 cycles a
  command once the input binds again.
- **LUTs.** The array costs about 1.8× `SystolicUnit`'s LUTs for 2.2× its throughput; the per-element
  selects are the next place to look.
- **Ordered, not addressed.** Operands are reused in order (`keep_a`), not by address. Addressed
  regions would need hand-written T2P BRAMs shared between tasks (`T2pBram`, `LockedT2pMemIF`).
