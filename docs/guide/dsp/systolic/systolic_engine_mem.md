---
title: Systolic engine behind memory
parent: Systolic Matrix Multiply
grand_parent: DSP Blocks
nav_order: 3
audience: python
api: [SystolicEngineMem]
summary: "SystolicEngineMem multiplies matrices larger than the systolic engine, in memory: one job descriptor in, C written back, an EngineDone out. Covers the job and its rules, the block-major memory layout, a runnable check of what it accepts, its architecture -- a sequencer running the output-stationary loop over C's tiles, a feeder that owns the read port and streams each tile's panels in bursts, the engine, a writer -- why it has a feeder of its own, what the RTL measured on a 32×32×64 product (Vitis 2025.1, centre), how to test it, and its limits."
snippets: run
---

# Systolic engine behind memory

`SystolicEngineMem` (`waveflow/linalg/systolic_engine_mem.py`) computes `C = q(A·B)` or `C = q(Aᴴ·B)`
for matrices larger than the [systolic engine](systolic_engine.md) holds, with the operands and the
result in memory. It *contains* a `SystolicEngine` -- same parameters, same arithmetic, bit-exact with
`waveflow.linalg.matmul.matmul` -- and adds the outer loop: it covers `C` in tiles of `Mmax × Nmax`,
each one engine command whose panels are read from memory as the engine takes them.

All numbers here: Vitis HLS / Vivado xsim **2025.1**, `xczu48dr-ffvg1517-2-e`, 4 ns, the engine's
centre configuration (`Mmax 8, Kmax 8, Nmax 32, Pmax 4, R 4, C 8, L 4`), 12-bit operands.

## The job

On `s_job`, one `EngineJobDesc` per product:

| Field | Meaning |
|---|---|
| `tx_id` | the job's id, echoed in its `EngineDone` |
| `op` | `MUL` or `MUL_AH` |
| `M`, `K`, `N` | `C` is `M × N`; `A` is `M × K` (`K × M` for `MUL_AH`), `B` is `K × N` |
| `a_base`, `b_base`, `c_base` | the first word of `A`, `B` and `C` in memory |

When the last tile of `C` is in memory, the job's `EngineDone` (`tx_id`, `status`) goes to `s_done`.

**The rules.** `M`, `K`, `N` are multiples of `Mmax`, `Kmax`, `Nmax`, and `K ≤ Pmax · Kmax`: the partial
sums of a tile stay on chip, so a longer `K` needs a larger `Pmax`. A job that breaks one is answered
`BAD_OP` or `BAD_DIMS` with no write. Pad other shapes with zeros.

```python
from waveflow.linalg.systolic import MatmulOp
from waveflow.linalg.systolic_engine_mem import EngineJobDesc, job_status

for tx, (M, K, N) in enumerate([(64, 32, 128), (64, 40, 128), (12, 8, 32)], start=1):
    d = EngineJobDesc()
    d.tx_id, d.op, d.M, d.K, d.N = tx, MatmulOp.MUL, M, K, N
    print(tx, job_status(d, Mmax=8, Kmax=8, Nmax=32, Pmax=4).name)
```

```text
1 OK
2 BAD_DIMS
3 BAD_DIMS
```

**Memory is block-major** (`block_major`), so each panel and tile is one burst: `A`'s panel `(i, p)` at
`a_base + (i·P + p)·wa`, `B`'s `(p, j)` at `b_base + (p·NT + j)·wb`, `C`'s tile `(i, j)` at
`c_base + (i·NT + j)·wc` -- `P = K/Kmax` panels, `NT = N/Nmax` column tiles, and `wa`, `wb`, `wc` the
words of one panel or tile. `read_c` reads `C` back into a matrix.

## Architecture

```mermaid
flowchart LR
    job["s_job"] --> SEQ["EngineSeq"]
    SEQ -->|"feed: a tile's panels"| FD["EngineFeeder (m_axi read)"]
    FD -->|"EngineCmd, A, B, A, B ..."| ENG["SystolicEngine"]
    SEQ -->|"tile"| WR["EngineWriter"]
    ENG --> WR
    WR -->|"MemWCmd | C"| MW["MemWStream (m_axi write)"]
    MW --> done["s_done"]
```

**The sequencer** checks the job and runs the output-stationary loop: per tile `(i, j)` of `C`, it sends
the feeder where `A(i, 0)` and `B(0, j)` are and the step between `B`'s panels, and the writer the tile's
`C` address and whether it is the job's last.

**The feeder** owns the read port. Per tile it writes the engine command (`npanels = K / Kmax`), then
`A(i,0) B(0,j) A(i,1) B(1,j) ...` -- the order the engine takes them -- a burst loop per operand panel, a
word a cycle.

**The writer** turns each tile's `Resp | C` from the engine into `MemWCmd(C(i, j)) | C` for the
framework's in-band memory writer, with the job's `EngineDone` behind its last tile. The `m_axi` reader
and writer of one generated top must fire equally often (their pointer FIFOs couple them): the feeder
fires once a tile and the writer writes once a tile, so they balance.

**Traffic.** Each tile reads its `A` row panels and `B` column panels, so `A` is read `N / Nmax` times and
`B` `M / Mmax` times ([memory](systolic_theory.md#memory)).

## Why a feeder of its own

Each reader measured on the `32×32×64` product with 64-bit words, its read port traced (before the
array ran continuously):

| Reader | Done at | Read port busy |
|---|---|---|
| the framework's in-band `MemRStream`, a firing per read command | 6,655 | 80%: ~16 idle cycles at each of 8 commands a tile |
| one flattened loop with a computed address | 10,639 | 49%: no burst inferred, a single-beat read every two cycles |
| a tile's `A` panels and `B` panels as two runs | 7,984 | 67%: `B` waits behind the core's last panel (one block of buffering) |
| **a burst loop per panel, in the engine's order** | **6,470** | 82%: ~12 idle cycles at each switch between burst loops |

## Measured

| What | 64-bit words | 128-bit words |
|---|---|---|
| the `test` jobs (a 2×2-tile 2-panel job, an `Aᴴ` job of 4 panels, a single tile) | `C` exact at RTL | `C` exact at RTL |
| `32×32×64` (8 tiles × 4 panels): done at | **6,468** | **3,680** |
| its input floor (each tile's 4 panels of `A` and `B`, a word a cycle) | ~5,136 (79%) | ~2,570 (70%) |
| resources | 157 DSP, 37.2k LUT, 41.7k FF, 36 BRAM18K, est. 3.35 ns | 157 DSP, 37.4k LUT, 43.8k FF, 44 BRAM18K, est. 3.35 ns |

**The read port sets the pace, not the array.** At 128-bit words a tile takes ~460 cycles while its four
panels need ~270 steps of the engine: the feeder's 321 words a tile and its idle cycles between burst
loops are the time. Making the engine's array continuous across panels moved the `32×32×64` product
from 3,693 to 3,680 cycles, and from 6,470 to 6,468 -- the gain is in the read port
([Limits](#limits)).

## Testing it

`tests/linalg/_engine_mem_harness.py` puts it in its own harness: a driver on `s_job`, a sink on
`s_done`, and one memory behind both `m_axi` ports, loaded with the operands and dumped after the run;
`check_c` compares each job's `C` in the dump with the model. Workloads: `test` (a 2×2-tile 2-panel job,
an `Aᴴ` job of 4 panels, a single tile) and `big` (`32×32×64`).

```
python -m tests.linalg._engine_mem_harness --root C:/wfsc/em --through compare --word-bits 128 --workload big
```

The gates are in `tests/linalg/test_systolic_engine.py`: the wrapper's pysim writes `C` exactly at both
word widths, and with `-m xsi` the RTL does, on `32×32×64`.

## Limits

- **`K ≤ Pmax · Kmax`.** No accumulation through memory yet: a longer `K` needs a larger `Pmax`.
- **Tiles are the maxima.** Every tile is `Mmax × Nmax`, `Kmax` per panel; other shapes pad.
- **The read port idles between bursts, and binds.** Each switch between the feeder's burst loops
  costs ~12 idle cycles, 8 switches a tile. Removing it needs bursts that overlap across loops (`hls::burst_maxi`,
  which the generated top does not yet declare) or deeper buffering of `B`, so `A` and `B` can each be
  read in one run.
- **No reuse across tiles.** A tile reads its panels whether or not the last tile read the same `A`.
- **Not calibrated.** Its time in pysim is the sum of its tasks' seeded times.
