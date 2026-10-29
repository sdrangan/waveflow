# Spec: LTBF on the shared-memory accelerator (`plans/ltbf_dse/ltbf_dse.md`, step 1.1)

**Status:** Draft for the 👁 review of step 1.4. Frozen when the user approves; its SHA-256 then goes
into the plan's §15, and every later step builds against it. A choice marked **[gate]** is a
recommendation the review must confirm or change.
**Inputs:** the plan (§1–§2), the LTBF paper and its code, and the spikes of step 1.2 (the plan's §15
and `plans/ltbf_dse/ltbf_dse_lessons.md`; scripts in `examples/ltbf_dse/spikes/`, results local in
`examples/ltbf_dse/data/spikes/`).

## 0. What the spikes fixed

| Spike | Result that shapes this spec |
|---|---|
| a, rendering | The factored path (`A @ D`, rfenv's own interpolator) equals `reference_apply` to 4e-16 and renders a 32x32 three-sector slot in 0.38 s: signals are rendered on the fly. 2.96% of UE-sector links spread beyond the CP after timing advance; none arrives early (one site). |
| b, systolic | `SystolicEngineMem` builds with Vitis HLS 2024.1 only with `config_compile -pipeline_style flp` (2024.1+flp). At `Pmax` 128 (K up to 1024): 144 DSP, 47,380 LUT, 53,572 FF, 44 BRAM18, 3.35 ns. Operand width ≤ 26 bits at K = 1024. Formats are fixed per build. Only `MUL` (`A·B`) and `MUL_AH` (`Aᴴ·B`). |
| c, vector unit | A pass writes a group once and reads/writes one group a bank; broadcast copies lane 0 to all lanes; `.vs` scalars are real; divides only on scalar lanes (`wf.sdivz`). A column's dot product needs all its rows in one block (`VLMAX ≥ N_rx`), and `Kmax·⌈Nmax/L⌉ ≤ 4096`. The per-iteration CG step works (8,679 cycles at 1024 × 8, extrapolated) and moves `7·k·n` elements. |
| d, memory | 7.56 MB on chip. The factored long-term set fits at every size (2.95 MB at 1024); explicit at 1024 needs 6.95 MB. Full-band ingest needs 5.7 / 22.7 / 91 128-bit ports at 64 / 256 / 1024 antennas. |
| e, host | A `SwHost`'s threads charge a `Processor`; padding each call to host cycles matches the C++ twin's `compute(n)` exactly with one scheduler thread. The C++-host XSI flow works on this machine after the `find_vitis_include_dir` fix. |

## 1. Signals (rendered by `examples/ltbf_dse/signals.py`, front end `frontend.py`)

**Numerology** (the paper's 100 MHz at 120 kHz): FFT 1024 at 122.88 Msps, CP 72 samples (0.586 µs;
the longer first CP of each 0.5 ms is not modeled), 792 active subcarriers (66 RBs) centred on DC,
a slot of 14 symbols (125 µs). Two snapshots per drop, `T_LT` = 10 ms apart: the drop's `a0, tau0,
valid0, k_rx0` (snapshot 0) and `a1, …` (snapshot 1).

**Channel.** Per sector, the synthetic array of `ltbf_link.channel` (one delay per (UE, path) shared by
the sector's antennas, the array phase in the amplitude), each UE timing-advanced to its serving
sector's earliest path, everything else as traced (delay spread beyond the CP stays as real ISI).
Doppler inside a snapshot is zero. Power control is `ltbf_link.power_control` (serving sector,
outage, the scale that puts a UE at its SINR target), with the noise normalized to **unit variance
per resource element after a unitary FFT**: complex white Gaussian noise drawn by sample index
(`waveflow.rfenv.noise.ThermalNoise`, seeded per drop, snapshot and sector).

**Snapshot 0: one SRS symbol** carrying all 30 UEs (every sector hears every UE):
- comb 4 (`K_TC` = 4), the four comb offsets used, **8 equally spaced cyclic shifts per comb**
  (`α_p = 2π p / 8`), 30 UEs on combs 0–3 as 8, 8, 7, 7. NR's comb 4 allows 12 shifts; 8 equally
  spaced ones are a stated simplification so every port's delay window is the same.
- the base sequence: Zadoff–Chu of prime length 197 extended cyclically to the 198 comb subcarriers;
- each UE's power spread over its 198 comb REs (4x the per-RE energy of a data symbol).

**Snapshot 1: one PUSCH slot.** DMRS on symbols 2 and 3 (NR type-2, double symbol: 3 CDM groups of
2 adjacent subcarriers in 6, FD-OCC of length 2 and TD-OCC of length 2, 12 ports); the 10 UEs of a
sector on ports 0–9, the same ports reused by the other sectors (pilot contamination, as deployed).
Data: uncoded QPSK on all 792 subcarriers of the other 12 symbols (9,504 REs a UE a slot). A UE
transmits at the same per-RE energy on DMRS and data.

**Front end** (stands for the O-RU, O-RAN 7.2x): per antenna, CP removal and a unitary 1024-point FFT,
the 792 active bins kept. It produces the grid in **tiles** (§5.4); in the system simulation the
Ingest DMA writes each tile into Shared Mem.

## 2. Channel estimation in Python, with op counts (`chest.py`, `opcount.py`)

`opcount.py` is a thin layer over numpy that every estimator calls for its arithmetic. Each call logs
`(stage, op, shapes, cmacs, divides, sqrts, bytes_in, bytes_out)`; a run's log sums to the stage's
complexity, and the spec's formulas below are what a test checks it against (plan AC4 c). Every
operation is written as a product of matrices of known shape, so it can later be priced on the
systolic engine's models.

**SRS → `Ĥⱼ` (per sector, per comb).** With `Y_c` the `N_rx × 198` received comb, `r` the base
sequence and `P` the `198 × (8·n_ports)` matrix of each port's windowed delay-domain filter
(IDFT, keep the port's `198/8`-tap window, DFT, then the 8 snapshot columns):

    E_c = Y_c ⊙ conj(r)ᵀ                        N_rx·198 cmacs
    Ĥ_c = E_c · P                                N_rx·198·8·n_ports cmacs (one product a comb)

`Ĥⱼ` (`N_rx × 8`) is port j's 8 columns: **`N_SRS` = 8 snapshots** at 8 comb subcarriers equally
spaced over the band. `Ĥα = [Ĥ₁, …, Ĥ₃₀]/√N_SRS` over the connected UEs; the estimator writes it into
Shared Mem in the layouts §5.3 lists (copies, logged as bytes). **Processing gain** for a flat channel:
the comb concentrates 4x the energy per RE and the window keeps 1/8 of the delay taps, so
`G_SRS = 10·log10(4·8) = 15.05 dB` below the data-domain noise floor (the paper used 10 dB; its code
measured ≥ 10.8 dB from the coherence bandwidth). AC4 (a) checks this number on a flat synthetic
channel; on the drops the achieved `G_SRS` is reported (multipath beyond a port's 260 ns window leaks
into the next port).

**DMRS → the effective channel `h̃'ᵢ[n]` (per sector, after the projection).** On the DMRS REs of the
whitened projection `z'ᵢ = G'ᵢ y` (§3): despread UE i's OCC over each 2-subcarrier × 2-symbol block of
its CDM group (one estimate per 6 subcarriers), interpolate linearly across frequency to all 792
subcarriers. `h̃'ᵢ[n]` is `r × 1`, constant over the slot. Then the MMSE weight (§3) **[gate E]**:

    wᵢ[n] = h̃'ᵢ[n] / (1 + ‖h̃'ᵢ[n]‖²)              r cmacs + 1 divide a (UE, subcarrier)

written per resource element in the layout the vector unit's equalizer reads (§5.3).

In the system simulation each estimator runs as a **zero-time** stage the host calls when its inputs
are in Shared Mem; its outputs are written there. In the RTL gate the C++ host replays them from a
file the pysim run writes.

## 3. The algorithm

**Long term, per sector, once per `T_LT`** (paper §III, code `compute_lt_bf`, regime 1 by default):

1. `Q̂ = I + ĤαĤαᴴ`. **Factored** `S = D + Ĥα(ĤαᴴD)`; **explicit** `K = ĤαĤαᴴ` formed once, `S = D + K D`.
2. Multi-right-hand-side CG on `Q̂Aᵢ = Ĥᵢ` from `X₀ = 0`, `R₀ = D₀ = Ĥᵢ`, `t` iterations, each column
   its own `ω, ν` (`ω = ‖r‖²/(dᴴs)`, `X += Dω`, `R -= Sω`, `ν = ‖r'‖²/‖r‖²`, `D = R + Dν`); a zero
   divisor gives 0 (`wf.sdivz`), the fixed-point form of the code's stop rule. Regime 2 (`Q̂X = I`, then
   `Aᵢ = XĤᵢ`) is a modeled DSE option.
3. `Mᵢ = sym(ĤᵢᴴAᵢ)` (8 × 8), Jacobi on the A53, `Φᵢ` its top-r eigenvectors.
4. **Whitening folded into the projection** **[gate W]**: with `Gᵢ = (AᵢΦᵢ)ᴴ`,
   `R̃ᵢ = GᵢGᵢᴴ + Σ_{j≠i} (GᵢĤα,ⱼ)(GᵢĤα,ⱼ)ᴴ = Φᵢᴴ [AᵢᴴAᵢ + Σ_{j≠i}(AᵢᴴĤα,ⱼ)(AᵢᴴĤα,ⱼ)ᴴ] Φᵢ` (r × r),
   `Φ'ᵢ = Φᵢ R̃ᵢ^{-H/2}`, and the projection actually applied is `G'ᵢ = Φ'ᵢᴴAᵢᴴ = R̃ᵢ^{-1/2}Gᵢ`.
   Its projected interference plus noise is white, so the equalizer below needs one real divide a
   (UE, subcarrier) and no r × r inverse per resource element, for r = 1 and r = 2 alike.

**Real time, every slot:** `z'ᵢ = G'ᵢ y` for every RE; `h̃'ᵢ[n]` from the DMRS REs of `z'ᵢ` (§2); and

    x̂ᵢ[n,k] = wᵢ[n]ᴴ z'ᵢ[n,k],   wᵢ[n] = h̃'ᵢ[n] / (1 + ‖h̃'ᵢ[n]‖²)

the MMSE estimate (`z' = h̃'x + n'`, `E[n'n'ᴴ] = I`), whose SINR is `‖h̃'ᵢ‖² = h̃ᵢᴴR̃ᵢ⁻¹h̃ᵢ`, the
paper's eq. `sinr_proj`.

**Measurement (Python):** per UE, the measured post-equalization SINR `1/mean|x̂ - x|² - 1` over its
data REs (unbiased by the MMSE's known bias), EVM, uncoded QPSK BER; and the paper's genie SINR
(`compute_sinr_lt`: true per-frequency `Q`, frequency mean of the linear `γ`). The DSE's accuracy metric
is the measured SINR's loss against the same chain in float64 with a direct solve (plan §2).

## 4. Fixed-point formats

- Every buffer in Shared Mem is a memory element of 16 + 16 bits (`lane_bits` 16) holding the stored
  integer of its format; a format is `(W, I)` with `W ≤ 16` in memory (wider registers inside the
  engines). The DSE's word width W (10–24) sets the engines' internal formats; memory stays 16-bit
  lanes, so W > 16 is an internal-precision axis only. **[gate F]**
- **Systolic:** one format triple `(a, b, c)` per build serves every job (long-term and real-time
  alike). W ≤ 26 at K = 1024 (`acc_format`); the c format's integer bits are set by the job with the
  largest range (`ĤαᴴD` or `Z`), the others lose those bits of precision. The accuracy DSE shows the
  cost; separate long-term and real-time builds are a modeled nice-to-have.
- **Vector unit:** a format per register group, scalar and queue (the CG step's centre at 12/16 bits as
  spike c; the equalizer's at 16).
- **Ranges** are profiled on held-out drops (as `paper/cg_dse`'s `ltbf_fixed` profile) per (target,
  array), with guard bits; a per-sector power-of-two scale on `Ĥα` (logged by the estimator) keeps
  `|Ĥα| < 1`.
- **CPU** (A53): float64 in C (Jacobi, `R̃`, `R̃^{-1/2}`, `Φ'`, `G'`); its output written to Shared Mem
  quantized to the systolic `a` format.

## 5. The system

### 5.1 Diagram (the plan's §1.2, with what the spikes added)

```
                IRQ (data)
  ┌──────────────────┐ ───────► ┌───────────────────────────┐  ┌───────────────────┐  ┌────────────────────────┐
  │ Resp FIFO        │          │ Host: SwHost + Processor  │  │ Ingest DMA        │  │ Shared Mem             │
  │ (RespHub:        │          │ A53 model, 1-4 cores:     │  │ tiles from the    │  │ MemoryMod, on-chip     │
  │  merge of 3 done │          │ scheduler, Jacobi, R~,    │  │ front end into a  │  │ BRAM/URAM; the DDR     │
  │  streams; a      │          │ Phi', G'; Python stages   │  │ ring of buffers   │  │ model prices the sets  │
  │  QueueOut view)  │          │ (zero time)               │  │ (MemWStream)      │  │ that do not fit        │
  └────▲───▲───▲─────┘          └─────────────┬─────────────┘  └─────────┬─────────┘  └───────────▲────────────┘
       │   │   │                              │ M0                       │ M1                      │ S
  ═════╪═══╪═══╪══════════════════════════════╪══════════ AXI-MM crossbar ═══════════════════════════╪══════
       │   │   │   S │ qjob  S │ ctrl   M2 ▲ rd  M3 ▲ wr      S │ qjob  S │ ctrl  M4 ▲ prog  M5 ▲ data  M6 ▲ wr
       │   │   │ ┌───▼──┐ ┌────▼──┐      │       │         ┌──▼───┐ ┌──▼────┐     │         │         │
       │   │   │ │JobFIFO│ │ Ctrl  │      │       │         │JobFIFO│ │ Ctrl  │     │         │         │
       │   │   │ └───┬──┘ └────┬──┘      │       │         └──┬───┘ └──┬────┘     │         │         │
       │   │   │ ┌───▼─────────▼─────────┴───────┴─┐       ┌──▼────────▼──────────┴─────────┴─────────┴─┐
       │   │   │ │ Systolic wrapper                 │       │ Vector-unit wrapper                        │
       │   │   │ │ SystolicEngineMem (feeder, writer)│       │ job decoder, program reader + splitter,    │
       │   │   │ │                                   │       │ data reader, VecUnit, writer, done join   │
       │   │   │ └──────────────┬────────────────────┘       └─────────────────────┬──────────────────────┘
       │   │   └── done ────────┘                                                  │
       │   └────── done (VecStatus + its writes in memory) ────────────────────────┘
       └────────── tile-ready (Ingest DMA) ── (Ingest DMA's Ctrl: ring base, tile bytes, depth, enable)
```

The RespHub is a device of its own (its `qresp` view). Bus masters: M0 host, M1 Ingest DMA, M2/M3 the systolic feeder and writer, M4/M5/M6 the vector unit's
program reader, data reader and writer (seven). Slaves: Shared Mem, both Job FIFOs, both Ctrls, the
Ingest DMA's Ctrl, the Resp FIFO. Each engine and the Ingest DMA is one `build_mm_device` device.

### 5.2 Address map (bytes; the host's endpoints come from `MemSlaveLayout.at(base)`)

| Device | Base | Views (4 KB each, in order) |
|---|---|---|
| Systolic wrapper | `0x0000_0000` | `qjob` (`QueueIn`, depth 16 words), `ctrl` (`RegBank`) |
| Vector-unit wrapper | `0x0000_4000` | `qjob` (`QueueIn`, depth 64 words), `ctrl` (`RegBank`) |
| Ingest DMA | `0x0000_8000` | `ctrl` (`RegBank`) |
| RespHub | `0x0000_C000` | `qresp` (`QueueOut`, depth 32 words) |
| Shared Mem | `0x1000_0000` | one window, size a knob (gate: 1 MB; DSE: up to 7.5 MB) |

### 5.3 The layout contract

`SystolicEngineMem` reads and writes **block-major** (`MUL`: A as row-major `Mmax × Kmax` blocks, row
block outer; `MUL_AH`: the stored A as `Kmax × Mmax` blocks, block column outer; B as `Kmax × Nmax`
panels, panel row outer; C as `Mmax × Nmax` tiles, tile row outer). The vector unit, the CPU and the
Ingest DMA read and write **row-major** blocks. Without a converter, a matrix moves between them only
where the two coincide, which is exactly when it is **one tile wide in its role**: `K = Kmax` for a
`MUL` A, `M = Mmax` for a `MUL_AH` A, `N = Nmax` for a B or a C. The build follows from that:

- **`Mmax = Kmax = 8`** (= `N̄`, one UE's SRS block), **`Nmax = c` ∈ {8, 16, 32}** (a knob; the RTL
  gate builds 8). A CG job solves `c/8` UEs at once (joint per job; accuracy-identical to per-UE).
- Every buffer and who writes it:

| Buffer | Shape | Layout | Written by | Read as |
|---|---|---|---|---|
| `Ĥα` (AH copy) | `N_rx × 8` per UE, concatenated | row-major per UE | estimator | `MUL_AH` A (`ĤαᴴD`, `ĤᵢᴴX`) |
| `Ĥα` (MUL copy) | `N_rx × 8U` | `8 × 8` blocks, row block outer | estimator | `MUL` A (`Ĥα T`, explicit `K`) |
| `Ĥα` (B copy) | `N_rx × 8U` | `8 × c` panels, panel row outer | estimator | `MUL_AH` B (`XᴴĤα`) |
| `Ĥαᴴ` (B copy, explicit only) | `8U × N_rx` | `8 × c` panels | estimator | `MUL` B (`K = ĤαĤαᴴ`) |
| `K` (explicit only) | `N_rx × N_rx` | C tiles = A blocks only when `c = 8` | systolic | `MUL` A (`K D`) |
| `D, X, R` | `N_rx × c` | row-major | vector unit | `MUL` B, vector unit |
| `X` (AH copy) | `N_rx × 8` per UE | row-major per UE | vector unit (last iteration, `L ≤ 8`) | `MUL_AH` A (`XᴴX`, `XᴴĤα`) |
| `T = ĤαᴴD` | `8U × c` | row-major (C, one tile wide) | systolic | `MUL` B |
| `U = Ĥα T` or `K D` | `N_rx × c` | row-major | systolic | vector unit |
| `M`, `XᴴX`, `XᴴĤα` | `c × c`, `c × c`, `c × 8U` | row-major / C tiles | systolic | CPU |
| `G'_stackᴴ` | `N_rx × 8` per 8 streams | row-major per group | CPU | `MUL_AH` A (`Z = G' Y`) |
| `Y` chunk | `N_rx × J·c` (J tiles) | B panels (`8 × c`, panel row outer): the order the front end streams it | Ingest DMA | `MUL_AH` B (`Z = G' Y`) |
| `Z` chunk | `S × J·c` (`S = r·U_pad` streams, r-major, `U_pad = 8⌈U/8⌉`) | C tiles (`8 × c`) | systolic | vector unit, a tile (`8` streams × `c` REs) a load |
| `w` chunk | `S × J·c` | C tiles, as `Z` | estimator | vector unit |
| `x̂` | `8 × c` a tile of 8 UEs | row-major | vector unit | measurement |
| `R₀ = D₀` | `N_rx × c` a CG job | row-major | estimator (a copy of the job's `Ĥᵢ`) | `MUL` B, vector unit |

- **Consequences.** The explicit `Q̂` is built only at `c = 8` (its `K` as the systolic writes it is
  the `MUL` A layout only then); at `c > 8` it is modeled. `G'` is formed on the CPU (`r·8·N_rx`
  cmacs a UE), which also writes it in the projection's layout; forming it on the systolic engine
  needs a layout converter (modeled). The vector unit's last CG iteration stores `X` twice at `c > 8`
  (needs `L ≤ 8` there).

### 5.4 Job descriptors, records and registers (`DataList`s; never packed by hand)

- **Systolic `qjob`**: `EngineJobDesc` as it is (`tx_id, op, M, K, N, a_base, b_base, c_base`); the
  wrapper passes it to `s_job` unchanged. Its `EngineDone` goes to the RespHub tagged `src = 0`.
- **Systolic `ctrl`** (thin, user 2026-10-28): config `SysCfg{enable}`; status `SysStatus{jobs_done,
  last_tx_id, last_status}` (a tap on the done stream).
- **Vector-unit `qjob`**: `VecMemJob{tx_id, prog_slot, n_ld, n_st}` then `n_ld` load addresses and
  `n_st` store addresses (word addresses in Shared Mem), in the order the job's `wf.ld` / `wf.st`
  instructions run.
- **Vector-unit `ctrl`**: config `VecCfg{enable, prog_base}`; a program table in Shared Mem at
  `prog_base`: per slot `{l_addr, l_words, x_addr, x_words, s_addr, s_words}`, the programs written
  once by the host at start. Status `VecStatus2{jobs_done, last_tx_id, last_status}`.
- **The vector-unit front end**: the job decoder turns a `VecMemJob` into a program fetch (slot →
  three bursts, the `VecJob` word of each rewritten with the job's `tx_id`), load commands and store
  commands; **one reader per program stream** through a splitter and a separate data reader (spike c's
  deadlock rule); the writer turns each `VecResp` + data into a memory write at its store address; the
  done join waits for the job's `VecStatus` **and** its last write's completion, then emits the done
  record (`src = 1`). An error status is fatal (the unit takes no more input).
- **Ingest DMA `ctrl`**: `IngestCfg{enable, ring_base, tile_words, ring_depth}`; status
  `IngestStatus{tiles, overruns}`. It writes tile `k` at `ring_base + (k mod ring_depth)·tile_words`
  and pushes `TileReady{seq = k}` (`src = 2`); a tile that finds the ring full is dropped and counted
  (the host frees a slot by writing `IngestCfg` … **[gate R]**: or a credit register).
- **RespHub**: a `FreeRunMod` merging the three done streams round-robin into one `QueueOut`; a record
  is one 64-bit word `Resp{src(2), tx_id(16), status(8), seq(16)}`.
- **Tile and chunk**: a tile is `c` subcarriers of one symbol on every antenna; the Ingest DMA writes a
  **chunk** of `J` tiles (a knob) as one buffer in the projection's B layout, which is the order the
  front end streams it, so one projection job covers `J·c` REs. One tile a job would mean about 1,400
  projection jobs a slot a sector at `c` = 8, far beyond what the A53 can dispatch.

### 5.5 Job graphs

**Long term, per sector** (regime 1, factored, `c/8` UEs a CG job; `t` iterations):

    [py] SRS estimate → Ĥα (copies) ──► for each CG job (c/8 UEs):
        D₀ = R₀ = Ĥ_job (the estimator's copy), X₀ = 0 (set by the first step's program)
        t × { SYS T = ĤαᴴD ; SYS U = Ĥα T ; VEC step }        (explicit, c = 8: SYS U = K D)
        SYS M = Ĥ_jobᴴ X ;  SYS XᴴX ;  SYS XᴴĤα
        CPU per UE: Jacobi(Mᵢ) → Φᵢ ; R̃ᵢ ; R̃ᵢ^{-1/2} ; Φ'ᵢ ; G'ᵢ written into G'_stackᴴ
    (explicit only, once: SYS K = ĤαĤαᴴ)

**Real time, per slot and sector:** per tile `k` (DMRS symbols first):

    INGEST chunk k (J tiles) → SYS Zₖ = G'_stack Yₖ ─► (DMRS chunks) [py] DMRS estimate → w
                                                    └► (data chunks) VEC, per 8-UE tile:
                                                         x̂ = Σ_r conj(w_r) ⊙ z_r

### 5.6 The scheduler (the plan's §1.4)

A list scheduler in the host's dispatcher thread: ready jobs (all predecessors done) go into their
engine's `qjob` while it has room; on the `qresp` data IRQ every record is popped and its successors
released. Two classes (real-time tiles before long-term jobs), earliest deadline first inside a class;
no preemption inside an engine job. A static memory plan per graph (buffer lifetimes, the ingest ring),
checked against the Shared Mem size before dispatch. Jobs in flight plus ring tiles ≤ the `qresp`
depth. Policies: dynamic (built) and static in-order (baseline). Costs on the A53: `sched_ops` (ready
list), `dispatch` (calibrated), the new `release` kernel (pre-registered gem5 campaign), Jacobi.

### 5.7 The host (spike e)

`LtbfHost(SwHost)`: a bus master `m`, IRQ inputs `qjob_sys`, `qjob_vec`, `qresp`; a `Processor`
(the A53 platform, `n_cores` a knob) its threads charge through `charge(func, ...)`, padded to whole
host cycles (`examples/ltbf_dse/spikes/spike_e_host.py`). Threads: the dispatcher (the scheduler) and
the CPU-job workers. The C++ twin `LtbfHostModel` runs the same scheduler with the **deterministic
policy** (static order, dependency waits), charges `compute(ceil(c·f_host/f_cpu))` per operation,
links `ltbf_eig.c` for Jacobi, and replays the Python stages' outputs and the CPU jobs' results from
the scenario file the pysim run writes. The RTL gate (plan AC9) compares each engine's `qjob` writes
and `ctrl` writes, each source's subsequence of `qresp` records, and the final Shared Mem.

## 6. Open choices for the review

| Id | Choice | Recommendation | Why |
|---|---|---|---|
| **E** | Where the equalizer's divide happens | **The estimator computes `wᵢ[n] = h̃'ᵢ/(1+‖h̃'ᵢ‖²)` (Python, counted); the vector unit applies `x̂ = Σ_r conj(w_r) ⊙ z_r` per RE.** Alternative: the vector unit computes `w` with scalar-lane divides; it needs `h̃'` with lanes = (UE, subcarrier) and then a transposition into the `Z` tile layout the ISA cannot do (a converter, modeled). | The ISA has no elementwise divide and no lane transposition (spike c); the per-RE application is the bulk of the work and is in hardware. |
| **W** | Whitening | **Fold `R̃ᵢ^{-1/2}` into `G'`** on the CPU. | One real divide a (UE, subcarrier) for r = 1 and 2, no r × r inverse per RE. |
| **F** | Memory element width | **16 + 16 bits in Shared Mem**; W > 16 only inside the engines. | The engines' `lane_bits` 16 and the memory budget of spike d. |
| **R** | Ingest flow control | **A credit register** in the Ingest DMA's `ctrl` the host bumps when it frees a ring slot. | No overwrite of a tile still in use; overruns counted, not silent. |
| **C** | `c` (columns a CG job, = `Nmax`) | **8 for the RTL gate; 8, 16, 32 in the DSE** (16 and 32 need `L ≤ 8` for `X`'s second copy). | The layout contract; explicit `Q̂` built at 8 only. |
| **G** | `G'` formation | **CPU** (A53, `r·8·N_rx` cmacs a UE); systolic modeled. | Layout (§5.3). |
| **P** | The paper's SRS simplification | **8 equally spaced cyclic shifts per comb** (NR allows 12). | Equal per-port windows; 30 UEs on one symbol. |
