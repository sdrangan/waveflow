# Spec: LTBF on the shared-memory accelerator (`plans/ltbf_dse/ltbf_dse.md`, step 1.1)

**Status:** **Frozen** 2026-10-28: v2, approved by the user at the 👁 review of step 1.4 with every
recommendation of §6 (gate E: the weights from the Python estimator). v1 was reviewed by a read-only
agent (3 blockers, 6 major, 12 minor findings, all acted on; §7). Its SHA-256 is in the plan's §15;
a change after this needs the user's yes and a new hash. **Amendment 1** (§4.1, approved by the user
2026-10-28 during step 3.1) changes the format scheme; the plan's §15 holds both hashes.
**Inputs:** the plan (§1–§2, §14), the LTBF paper and its code, the spikes of step 1.2 (scripts in
`examples/ltbf_dse/spikes/`, results local in `examples/ltbf_dse/data/spikes/`; the plan's §15; the
lessons file).

## 0. What the spikes fixed

| Spike | Result that shapes this spec |
|---|---|
| a, rendering | The factored path (`A @ D`, rfenv's interpolator) equals `reference_apply` to 4e-16, renders a 32x32 three-sector slot in 0.38 s: rendering is on the fly. On a real slot, links within the CP render to within -54.7 dB of the ideal; 2.96% of links spread beyond the CP (ISI kept). |
| b, systolic | `SystolicEngineMem` builds with Vitis HLS 2024.1 only with `config_compile -pipeline_style flp` (2024.1+flp); at `Pmax` 128: 144 DSP, 47,380 LUT, 3.35 ns. Formats fixed per build; an operand is at most the memory lane (16 bits, or 32: `mem_format`); accumulator W ≤ 26 at K = 1024. Only `MUL` (`A·B`) and `MUL_AH` (`Aᴴ·B`). |
| c, vector unit | Pass rules (a group written once a pass, one group a bank; broadcast is lane 0 to all lanes; `.vs` scalars real; divides on scalar lanes only). A column's dot product needs all its rows in one block (`VLMAX ≥ N_rx`; `Kmax·⌈Nmax/L⌉ ≤ 4096`). The per-iteration CG step: 8,679 cycles at 1024 x 8 (extrapolated), memory-bound (moves `7kn` elements). The gate-E equalizer (§3): legal, bit-exact on the interpreter, 0.42–1.55 UE·RE a cycle (`c`, `L`, `r`). |
| d, memory | Usable on chip about 6.7 MiB (1,080 BRAM36 x 32 Kb + 80 URAM x 256 Kb of data, not the 7.56 MB raw). Full-band ingest needs 5.7 / 22.7 / 91 128-bit ports at 64 / 256 / 1024 antennas. The engines' own memories come out of the same BRAM (the vector unit's CG queues alone about 1.8 MB at N_rx 1024, `c` 32). |
| e, host | A `SwHost`'s threads charge a `Processor`; padded to host cycles, the C++ twin's `compute(n)` matches exactly with one scheduler thread. The C++-host XSI flow works here after the `find_vitis_include_dir` fix. |
| f, SRS (after review) | With 198 comb subcarriers the 8 cyclic shifts fall on fractional taps: ports leak -10 to -20 dB even on a flat channel. With **192** a whole-tap path separates exactly (-277 dB); a path a quarter / half tap off leaks -22.8 / -20.0 dB into other ports with a 4-tap lead and 4-tap taper (-10.4 dB with a plain window). |

## 1. Signals (`signals.py`, `frontend.py`; partly built in step 2.1)

**Numerology:** FFT 1024 at 122.88 Msps, CP 72 (0.586 µs; the longer first CP of each 0.5 ms not
modeled), 792 active subcarriers (66 RBs) centred on DC, 14 symbols a slot (125 µs). Two snapshots a
drop, `T_LT` = 10 ms apart (the drop's `*0` and `*1` arrays).

**Channel:** a sector's synthetic array (`ltbf_link.channel`), each UE scaled by its power control and
timing-advanced to its serving sector's earliest path, everything else as traced (ISI beyond the CP
kept), Doppler zero inside a snapshot. Noise: `waveflow.rfenv.noise.ThermalNoise`, unit power a
sample, so unit variance a resource element after the unitary FFT, seeded by (drop, snapshot, sector).

**Snapshot 0, one SRS symbol** for all 30 UEs (every sector hears every UE):
- **64 RBs sounded** (`m_SRS` = 64, a valid NR bandwidth), centred: **192 comb subcarriers** a comb;
- comb 4 (`K_TC` = 4), the four comb offsets, **8 cyclic shifts a comb** (`α_p = 2πp/8`, exactly 24
  delay taps apart at 192); 30 UEs on combs 0–3 as 8, 8, 7, 7, cyclic shift `p` = the UE's rank on its
  comb **[gate P]**;
- base sequence: Zadoff–Chu of length 191 extended cyclically to 192;
- each UE's power over its 192 comb REs: per-RE amplitude 2 (4x the energy of a data RE).

**Snapshot 1, one PUSCH slot:** symbols 0, 1, 4–13 data (uncoded QPSK, all 792 subcarriers: 9,504
data REs a UE); symbols 2, 3 DMRS (NR type-2, double symbol: 3 CDM groups of 2 adjacent subcarriers
in 6, FD-OCC 2 x TD-OCC 2, 12 ports; a sector's 10 UEs on ports 0–9, reused by every sector). No data
on the DMRS symbols, so the DMRS is boosted **4.77 dB** as NR does with 3 CDM groups **[gate D]**.

**Front end** (the O-RU, O-RAN 7.2x): per antenna, CP removal and the unitary FFT, 792 bins kept;
it delivers each symbol **antenna by antenna** (as 7.2x does) to the Ingest DMA (§5.4).

## 2. Channel estimation in Python, with op counts (`chest.py` on `opcount.py`)

Every operation goes through `opcount.OpLog` (built: logs stage, op, shapes, complex MACs, divides,
square roots and bytes; 2 tests). Closed-form counts below are what AC4 (c) checks.

**SRS → `Ĥⱼ`, per sector and comb.** `Y_c` (`N_rx × 192`), `r` the base sequence, and `P`
(`192 × 8·n_ports`) each port's linear filter: de-rotate, IDFT, keep its window (24 taps, a 4-tap
lead, a 4-tap raised-cosine taper each side), DFT, remove its phase ramp, divide the comb's amplitude
2, and take the 8 snapshot subcarriers (every 24th comb subcarrier):

    E_c = Y_c ⊙ conj(r)ᵀ          N_rx·192 cmacs
    Ĥ_c = E_c · P                  N_rx·192·8·n_ports cmacs        (a product a comb)

`Ĥⱼ` is port j's 8 columns, `N_SRS` = 8 snapshots; `Ĥα = [Ĥ₁, …, Ĥ₃₀]/√N_SRS` over the connected
UEs, written into Shared Mem in the layouts of §5.3 (copies, counted as bytes). **`G_SRS` = 16.14 dB**
for this window (4 / its noise gain 0.0973; spike f), against the paper's 10 dB nominal. AC4 (a):
on a synthetic flat channel with whole-tap delays, cross-port leakage ≤ -30 dB (spike f: -277 dB) and
the error variance within ±1 dB of 16.14 dB; on the drops, the achieved `G_SRS` and leakage are
reported (fractional delays leak about -20 dB a port).

**DMRS → `wᵢ[n]`, per sector, after the projection.** On UE i's DMRS REs of the whitened `z'ᵢ`
(`r × …`, §3): despread its OCC over each 2-subcarrier x 2-symbol block of its CDM group (one
estimate per 6 subcarriers, 132 a UE: `4r` cmacs each); estimate the UE's mean delay from the phase
slope between neighbouring estimates and remove it (`r` cmacs a block); interpolate linearly across
frequency (`2r` cmacs a subcarrier) with the nearest estimate at the band edges; restore the delay
ramp (`r` cmacs a subcarrier). Then **[gate E]** the MMSE weight

    wᵢ[n] = h̃'ᵢ[n] / (1 + ‖h̃'ᵢ[n]‖²)        r cmacs + 1 divide a (UE, subcarrier)

stored per resource element of one symbol's chunk in the `Z` layout (§5.3), reused by every data
symbol (constant over the slot).

**Placement in the system simulation:** each estimator is a **zero-time** compute stage the host
runs when its inputs are in Shared Mem; its inputs are read and its outputs written **over the host's
bus master** (timed, the same in both realizations). In the RTL gate the C++ host replays its outputs
from the scenario file the pysim run writes.

## 3. The algorithm

**Long term, per sector, once per `T_LT`** (regime 1; the paper's code `compute_lt_bf`):

1. `Q̂ = I + ĤαĤαᴴ`; **factored** `S = D + Ĥα(ĤαᴴD)`, **explicit** `K = ĤαĤαᴴ` once and `S = D + KD`.
   The SRS noise inflates `Q̂` by about `(U/G_SRS)·I` (0.73 at U = 30, 16.14 dB); kept as the paper
   does **[gate Q]**.
2. Multi-right-hand-side CG on `Q̂Aᵢ = Ĥᵢ`, `c/8` UEs a job, from `X₀ = 0`, `R₀ = D₀ = Ĥ_job`, `t`
   iterations, per column `ω = ‖r‖²/(dᴴs)`, `X += Dω`, `R -= Sω`, `ν = ‖r'‖²/‖r‖²`, `D = R + Dν`; a zero
   divisor gives 0 (`wf.sdivz`), the fixed-point form of the code's stop rule. Regime 2 modeled.
3. **One product for the rest:** `W = ĤαᴴX` (`8U × c`). Then `Mᵢ = √N_SRS · W[UE i's rows, UE i's
   columns]` (8 x 8, symmetrized), and `AᵢᴴĤα,ⱼ = W[j's rows, i's columns]ᴴ`.
4. On the A53, per UE: Jacobi on `Mᵢ` → `Φᵢ` (top r); `Gᵢᴴ = AᵢΦᵢ` (`8r·N_rx` cmacs, reading `Aᵢ`);
   `R̃ᵢ = GᵢGᵢᴴ + Σ_{j≠i} (ΦᵢᴴW[j,i]ᴴ)(ΦᵢᴴW[j,i]ᴴ)ᴴ` (`r²·N_rx + …` cmacs); **whitening folded into
   the projection [gate W]**: the Cholesky factor `R̃ᵢ = LᵢLᵢᴴ`, `G'ᵢ = Lᵢ⁻¹Gᵢ` (`r²·N_rx` cmacs).
   `G'ᵢ`'s interference plus noise is white.

**Real time, every slot:** `z'ᵢ = G'ᵢy` for every RE; the DMRS estimate and `wᵢ[n]` (§2); and

    x̂ᵢ[n,k] = Σ_r conj(wᵢ,r[n]) z'ᵢ,r[n,k]

the MMSE estimate for `z' = h̃'x + n'`, `E[n'n'ᴴ] = I`; its SINR `‖h̃'ᵢ‖² = h̃ᵢᴴR̃ᵢ⁻¹h̃ᵢ` is the
paper's eq. `sinr_proj`.

**Measurement (Python):** per UE over its data REs, (a) `1/mean|x̂ - x|² - 1` -- the MMSE SINR, of the
biased estimate as it comes out (not debiased first); (b) the scale-invariant `|ρ|²/(1 - |ρ|²)`, ρ the
normalized correlation of `x̂` and `x`; EVM; uncoded QPSK BER; and the paper's genie SINR
(`compute_sinr_lt`). The DSE's accuracy metric is (a)'s loss against the same chain in float64 with a
direct solve; (b) is reported beside it (it does not count a gain error as loss).

## 4. Fixed-point formats

**Memory elements:** `lane_bits` 16 (default) or 32 **[gate F]** -- a DSE axis: 32 doubles every
buffer and every word moved, and is what operands wider than 16 bits need (`mem_format` refuses an
operand wider than the lane). A value is stored as the integer of its producer's format; a consumer
reads it in its own format, so the two must have **the same integer bits** (else the value is scaled
by `2^ΔI`) and the consumer at least the producer's width.

**One systolic triple `(a, b, c)` per build.** The hand-offs force:

| Buffer | Producer (format) | Consumer (format) | Constraint |
|---|---|---|---|
| `Ĥα` copies | estimator | systolic A (`a`) | written in `a` |
| `D`, `X` | vector unit queue `D`, `X` | systolic B (`b`) | `I_D = I_X = I_b` |
| `T = ĤαᴴD` | systolic C (`c`) | systolic B (`b`) | `I_c = I_b` |
| `U` | systolic C (`c`) | vector unit queue `U` | `I_U = I_c` |
| `K` (explicit) | systolic C (`c`) | systolic A (`a`) | `I_a = I_c` |
| `W` | systolic C (`c`) | CPU (float) | none |
| `G'_stackᴴ` | CPU | systolic A (`a`) | written in `a` |
| `Y` | Ingest DMA (`y`) | systolic B (`b`) | `I_y = I_b` |
| `Z` | systolic C (`c`) | vector unit queue `Z` | `I_Z = I_c` |
| `w` | estimator | vector unit queue `w` | written in that format |

So a factored build has one integer-bit count for `b` and `c` (and `a` too, explicit), and one
`(I, W)` serves the long-term and the real-time jobs; the accuracy DSE shows what that costs (two
builds are a modeled nice-to-have).

**Per-sector scale:** the estimator stores `Ĥα' = 2^{-s}Ĥα` (`s` chosen so `|Ĥα'| < 1`, logged).
Then `S = D + Ĥα(ĤαᴴD) = 2^{2s}(2^{-2s}D + U')`: CG is solved in the scaled system, the vector unit
forms `S' = 2^{-2s}D + U'` with its result shift (`rs_max ≥ 2·s_max`; `s` a field of the vector
unit's Ctrl), and `Aᵢ` comes out scaled by a known `2^{s}` the CPU removes.

**Ranges** profiled on held-out drops per (target, array) with guard bits (as `paper/cg_dse`'s
`ltbf_fixed`); **CPU** float64 in C, its outputs quantized to `a`.

### 4.1 Amendment 1 (step 3.1, approved 2026-10-28): what one format triple can hold

Building the bit-exact model showed that one shared `bc` collapses values to zero: CG's `X` outgrows
`D` by about `1/λ_min` (6.8 against 0.08 on drop 1 at 8x8), and the real-time `Y` and `Z` are about
100 times the long-term values. So, with every scale a power of two chosen from the profiled ranges
and removed exactly downstream:

1. **`X` keeps its own format** (register and queue) in the CG state, which never enters the systolic
   engine; the **last CG iteration also stores `X_b = q_bc(2^-m X)`** (a `wf.vmacc.vs` with the
   result shift `m` into a zeroed group, then a store to a `bc`-format output), the B operand of
   `W = Ĥα'ᴴX_b`.
2. **The ingest writes `2^-k Y`** (its digital gain, a field of `IngestCfg`) and **the CPU writes
   `2^-g G'`**, so `Y` and `Z` share `bc` with the long-term values.
3. **The DMRS estimator** reads the stored `Z` times `2^(g+k)` and writes `2^(g+k) w`, so the
   equalizer's `x̂ = Σ conj(w) z` is unscaled.
4. **The CPU job** forms `Mᵢ` and `ĤαᴴAᵢ` from `W` times `2^(s+m)` and `Gᵢ` from the stored `X`.

The vector unit's `rs_max` is then `max(2·s_max, m_max)` (at most 15). Measured on drop 1, 8x8,
`t = 3`, against float CG: median loss 0.001 / 0.016 / 0.71 / 2.40 dB at W = 16 / 14 / 12 / 10.

## 5. The system

### 5.1 Diagram

```
                 IRQ (data)
  ┌──────────────────┐ ──────► ┌──────────────────────────┐  ┌──────────────────────┐  ┌────────────────────────┐
  │ Resp FIFO        │         │ Host: SwHost + Processor │  │ Ingest DMA           │  │ Shared Mem             │
  │ RespHub: merges  │         │ A53 model, 1-4 cores:    │  │ corner-turn buffer   │  │ MemoryMod, on chip     │
  │ 3 done streams,  │         │ scheduler, Jacobi, G, R~,│  │ (8 antennas x 792),  │  │ (about 6.7 MiB usable, │
  │ a QueueOut view  │         │ Cholesky, G'; Python     │  │ a ring of chunks,    │  │ less the engines' own) │
  │                  │         │ stages (zero time)       │  │ CreditIn for slots   │  │ DDR model beyond       │
  └───▲───▲───▲──────┘         └────────────┬─────────────┘  └──────────┬───────────┘  └───────────▲────────────┘
      │   │   │                             │ M0                        │ M1                       │ S
  ════╪═══╪═══╪═════════════════════════════╪══════════════ AXI-MM crossbar ══════════════════════════╪═══════════
      │   │   │   S│qjob S│ctrl  M2▲rd M3▲wr       S│qjob S│ctrl  M4▲lprog M5▲xprog M6▲sprog M7▲data M8▲wr
      │   │   │ ┌──▼─┐ ┌──▼─┐    │     │         ┌──▼─┐ ┌──▼─┐     │        │        │        │       │
      │   │   │ │Job │ │Ctrl│    │     │         │Job │ │Ctrl│     │        │        │        │       │
      │   │   │ └──┬─┘ └──┬─┘    │     │         └──┬─┘ └──┬─┘     │        │        │        │       │
      │   │   │ ┌──▼──────▼──────┴─────┴──┐      ┌──▼──────▼───────┴────────┴────────┴────────┴───────┴─┐
      │   │   │ │ Systolic wrapper         │      │ Vector-unit wrapper: decoder (affine descriptors),   │
      │   │   │ │ SystolicEngineMem        │      │ three program readers, a data reader, VecUnit, the   │
      │   │   │ │ (feeder, writer)         │      │ writer, the done join                                │
      │   │   │ └───────────┬──────────────┘      └─────────────────────────┬────────────────────────────┘
      │   │   └── done ──────┘                                              │
      │   └────── done (VecStatus and its last write) ──────────────────────┘
      └────────── chunk-ready (Ingest DMA; its Ctrl: ring base, chunk words, ring depth, enable)
```

Masters: M0 host, M1 Ingest DMA, M2/M3 the systolic feeder and writer, M4–M6 the vector unit's three
program readers, M7 its data reader, M8 its writer (nine). Slaves: Shared Mem, the two Job FIFOs, the
two engine Ctrls, the Ingest DMA's Ctrl and credit window, the Resp FIFO. Each engine, the Ingest DMA
and the RespHub is one `build_mm_device` device.

### 5.2 Address map (bytes; the host's endpoints from `MemSlaveLayout.at(base)`)

| Device | Base | Views (4 KB each, in order) |
|---|---|---|
| Systolic wrapper | `0x0000_0000` | `qjob` (`QueueIn`, 16 words), `ctrl` (`RegBank`) |
| Vector-unit wrapper | `0x0000_4000` | `qjob` (`QueueIn`, 32 words), `ctrl` (`RegBank`) |
| Ingest DMA | `0x0000_8000` | `ctrl` (`RegBank`), `credit` (`CreditIn`) |
| RespHub | `0x0000_C000` | `qresp` (`QueueOut`, 64 words) |
| Shared Mem | `0x1000_0000` | one window; its size a knob (gate: 1 MiB; DSE up to 6.7 MiB less the engines') |

### 5.3 The layout contract

`SystolicEngineMem` addresses block-major (`systolic_engine_mem.py:197–208`): `MUL`'s A as row-major
`Mmax × Kmax` blocks, row block outer; `MUL_AH`'s stored A as `Kmax × Mmax` blocks, block column
outer; B panel `(p, j)` (`Kmax × Nmax`) at `b_base + (p·NT + j)·wb`; C tile `(i, j)` at
`c_base + (i·NT + j)·wc`. The vector unit, the CPU and the estimators read and write row-major. A
matrix passes between them unconverted only when it is **one tile wide in its role** (`K = Kmax`
for a `MUL` A, `M = Mmax` for a `MUL_AH` A, `N = Nmax` for a B or C). Hence:

- **systolic maxima:** `Kmax = Nmax = c` (columns a CG job, ∈ {8, 16, 32}; RTL gate 8), `Mmax` free
  (∈ {8, 16, 32}; `Mmax = S`, the real-time streams, avoids re-reading `Y` a row tile at a time).
  (These are the systolic engine's; the vector unit's own `Kmax`/`Nmax` are written `VU.Kmax`,
  `VU.Nmax` below.)
- every buffer:

| Buffer | Shape | Layout | Written by | Read as |
|---|---|---|---|---|
| `Ĥα` AH copy | `N_rx × 8U` | `N_rx × Mmax` row-major groups, one after another | estimator | `MUL_AH` A (`T = ĤαᴴD`, `W = ĤαᴴX`) |
| `Ĥα` MUL copy | `N_rx × 8U` | `Mmax × c` blocks, row block outer | estimator | `MUL` A (`U = ĤαT`; explicit `K`) |
| `Ĥαᴴ` B copy (explicit) | `8U × N_rx` | `c × c` panels | estimator | `MUL` B (`K`) |
| `K` (explicit) | `N_rx × N_rx` | C tiles = `MUL` A blocks (`Nmax = Kmax`) | systolic | `MUL` A (`U = KD`) |
| `R₀ = D₀` | `N_rx × c` a job | row-major | estimator | `MUL` B, vector unit |
| `D`, `X`, `R` | `N_rx × c` | row-major | vector unit | `MUL` B; vector unit |
| `T` | `8U × c` | row-major (C one tile wide) | systolic | `MUL` B |
| `U` | `N_rx × c` | row-major | systolic | vector unit |
| `W` | `8U × c` | row-major | systolic | CPU |
| `G'_stackᴴ` | `N_rx × S` (`S = r·U_pad`, `U_pad = 8⌈U/8⌉`, r-major) | `N_rx × Mmax` row-major groups | CPU | `MUL_AH` A (`Z = G'Y`) |
| `Y` chunk | `N_rx × J·c` | B panels (`c`-wide panels, panel row outer) | Ingest DMA | `MUL_AH` B |
| `Z` chunk | `S × J·c` | C tiles (`Mmax × c`) | systolic | vector unit: per tile, `r` blocks of `U_pad × c` |
| `w` | `S × J·c` (one symbol's chunk) | as `Z` | estimator | vector unit |
| `x̂` | `U_pad × c` a tile | row-major | vector unit | measurement |

Padding: `8U` (240) to a multiple of `Mmax` and `c`; `N_rx` to `Kmax`; `S` to `Mmax`. **Memory**
(per sector, 16-bit lanes): two `Ĥα` copies (three explicit), `K` (explicit), one CG job's `X, R, D, T,
U, W`, `G'_stack`, the ring of `Y` chunks, the `Z`/`w`/`x̂` chunks and the data symbols' `Z` held
until the DMRS weights exist (symbols 0–1 come before the DMRS) -- checked by the memory plan (§5.6).

### 5.4 Descriptors, records and registers (`DataList`s; never packed by hand)

- **Systolic `qjob`:** `EngineJobDesc` (`tx_id, op, M, K, N, a_base, b_base, c_base`), passed to
  `s_job` unchanged; `EngineDone` to the RespHub tagged `src = 0`. **`ctrl`** (thin, user 2026-10-28):
  `SysCfg{enable}`; `SysStatus{jobs_done, busy_cycles, last_tx_id, last_status}`.
- **Vector-unit `qjob`:** a fixed-size **affine** descriptor `VecMemJob{tx_id, prog_slot, s}` then, for
  each load stream and each store stream, `{base, stride, count}` (word addresses; the `i`-th `wf.ld`
  of the stream reads its `k·n` elements at `base + i·stride`). Its size is a constant of the slot, at
  most the `qjob` depth, so the host's `q.write` waits for room and never stalls the bus mid-packet.
- **Vector-unit `ctrl`:** `VecCfg{enable, prog_base}`; the program table at `prog_base` in Shared Mem
  (per slot: address and length of its load, execute and store programs, written once at start);
  `VecStatus2{jobs_done, busy_cycles, last_tx_id, last_status}`.
- **The vector-unit front end:** the decoder turns a descriptor into one command per stream; **three
  program readers** (one per program stream: a `VecJob` word's `tx_id` rewritten to the job's), **a
  data reader** (the load streams' affine walks, in the load program's order) and **the writer** (each
  `VecResp` + data to its store stream's next address). Every stream has its own reader and a command
  of constant size a job, so no stream waits behind another (the v1 single reader + splitter
  deadlocked; spike-c lesson). **The done join** emits `Resp{src = 1}` when the job's `VecStatus` and
  its last write's completion have both arrived. An error status is fatal.
- **Ingest DMA:** a **corner-turn buffer** of `Kmax` antennas x 792 subcarriers (ping-pong): the O-RU's
  antenna-by-antenna symbol fills it; each full group of `Kmax` antennas is written as the chunk's
  panels, panel `(p, j)` at `chunk_base + (p·J + j)·wb` (`J` bursts of `Kmax·c` elements). A chunk is
  `J·c` subcarriers of one symbol on every antenna (along frequency); a ring of `ring_depth` chunks.
  When a chunk's last panel is written it pushes `ChunkReady{seq}` (`src = 2`). Flow control: the
  framework's `CreditIn` view (`mm_device.py`): the host returns a credit per freed chunk; with none,
  the incoming symbol is dropped and counted (`IngestStatus{chunks, overruns}`). `IngestCfg{enable,
  ring_base, chunk_words, ring_depth}`.
- **RespHub:** a `FreeRunMod` merging the three done streams round-robin into one `QueueOut`; a record
  is one 64-bit word `Resp{src(2), tx_id(16), status(8), seq(16)}`.

### 5.5 Job graphs

**Long term, per sector** (factored; `c/8` UEs a CG job; `t` iterations; a long-term product split
into `B_LT`-row jobs, the plan's tile-size knob, so a real-time job never waits behind more than one):

    [py] SRS estimate → Ĥα' (copies), R₀ = D₀ per job, scale s        (written over the host's bus)
    (explicit, once: SYS K = Ĥα'Ĥα'ᴴ)
    per CG job:  t × { SYS T = Ĥα'ᴴD ; SYS U = Ĥα'T ; VEC step S' = 2^{-2s}D + U, … }
                 SYS W = Ĥα'ᴴX
                 CPU per UE: Jacobi(Mᵢ) ; Gᵢ = (AᵢΦᵢ)ᴴ ; R̃ᵢ ; Cholesky ; G'ᵢ → G'_stackᴴ

**Real time, per slot and sector:** chunk by chunk along frequency, symbol by symbol:

    INGEST chunk → SYS Z = G'_stack Y ─► DMRS symbols 2–3: [py] DMRS estimate → w (per chunk position)
                                     └─► data symbols: VEC x̂ = Σ_r conj(w_r) ⊙ z_r, per tile
    (symbols 0–1 arrive before the DMRS: their Z chunks wait in Shared Mem for w)

### 5.6 The scheduler (the plan's §1.4)

The host's dispatcher thread runs a list scheduler: ready jobs go into their engine's `qjob` while it
has room; on the `qresp` data IRQ every record is popped and its successors released; a freed ring
chunk returns a credit. Two classes (real-time chunks before long-term jobs), earliest deadline first
inside a class; no preemption inside an engine job (hence `B_LT`). A static memory plan per graph
(buffer lifetimes, the ring, the held data symbols), checked against Shared Mem before dispatch. Jobs
in flight plus ring chunks ≤ the `qresp` depth. Policies: dynamic (built) and static in-order
(baseline). **A53 costs:** `sched_ops`, `dispatch` (calibrated); `release` (new); and the per-UE CPU
job of §3.4 as a new kernel `ltbf_proj` (Jacobi from `M`, `G`, `R̃`, Cholesky, `G'`), calibrated by a
pre-registered gem5 campaign like `paper/cg_dse`'s `ltbf_eig`. **Assumption:** the A53 reads `Aᵢ` and
`W` and writes `G'` through the bus model's timing (the SwHost's bus master), not an HPM port's
uncached-load latency, which on the real chip is much slower a word; stated with every time result.

### 5.7 The host (spike e)

`LtbfHost(SwHost)`: bus master `m`; IRQ inputs `qjob_sys`, `qjob_vec`, `qresp`; a `Processor` (`n_cores`
a knob) its threads charge through `charge(func, …)`, padded to host cycles. Threads: the dispatcher
and the CPU-job workers. **RTL gate (AC9):** one thread on one core, the deterministic policy (static
order, dependency waits); the C++ twin `LtbfHostModel` **replays** every CPU job's and Python stage's
results and charged cycles from the scenario file the pysim run writes (no CPU arithmetic in C++),
writes them over the bus as pysim does, and charges `compute(n)`. The gate compares each engine's
`qjob` and `ctrl` writes, each source's subsequence of `qresp` records, and the final Shared Mem.

## 6. Open choices for the review

| Id | Choice | Recommendation | Why |
|---|---|---|---|
| **E** | Where the equalizer's divide happens | The estimator computes `w = h̃'/(1+‖h̃'‖²)` (Python, counted); the vector unit applies `x̂ = Σ conj(w) ⊙ z` per RE (spike: 0.42–1.55 UE·RE a cycle). | The ISA has no elementwise divide and no lane transposition; computing `w` in hardware needs a layout converter (modeled). |
| **W** | Whitening | Fold `L⁻¹` (Cholesky of `R̃`) into `G'` on the CPU. | One real divide a (UE, subcarrier) for r = 1 and 2; Cholesky is cheaper than the Hermitian root and whitens as well. |
| **F** | Memory lane | 16 bits by default, 32 as a DSE axis. | Wider operands need it (`mem_format`); it doubles memory and traffic. |
| **Q** | SRS noise in `Q̂` | As the paper (kept); `S = βD + U` with `β = 1 - U/G_SRS` as an accuracy option. | Fidelity to the paper first. |
| **P** | SRS layout | 64 RBs, comb 4, 8 shifts, lead 4, taper 4 (`G_SRS` 16.14 dB); two SRS symbols (4 shifts a comb, 48-tap windows) as an option. | Spike f. |
| **D** | DMRS power | Boost 4.77 dB (NR, 3 CDM groups, no data). | As deployed. |
| **C** | `c`, `Mmax`, `J`, ring depth | `c` ∈ {8, 16, 32}, `Mmax` ∈ {8, 16, 32}, `J` and ring depth knobs; RTL gate: `c = 8`, `Mmax = 16`, `J = 4`, ring depth 4. | Layout contract; a small gate. |
| **G** | `G'` formation | CPU (`ltbf_proj`, calibrated); systolic modeled. | Layout. |

## 7. Review of v1 (2026-10-28) and what changed

Blockers: **B1** the `Y` chunk's B layout (panel `(p, j)` at `(p·NT + j)·wb`) cannot come from
tile-contiguous writes → the corner-turn buffer, chunk-level ring and ready records (§5.4). **B2** a
single program reader + splitter and variable-length address lists deadlock → three program readers,
a data reader, affine fixed-size descriptors (§5.4). **B3** 198 comb subcarriers leak -7 to -20 dB
between ports → 192, lead and taper, `G_SRS` re-derived (spike f). Major: **M1** formats → the table of
§4 and the lane axis; **M2** `W = ĤαᴴX` replaces three products, frees `Mmax`, drops the `Ĥα` B copy
and `X`'s second copy, explicit `Q̂` at every `c`; usable memory 6.7 MiB less the engines'; **M3** long-term
jobs split (`B_LT`); **M4** Python and CPU results reach Shared Mem over the host's bus and are replayed
in C++, one thread at the gate; **M5** the CPU work as the calibrated `ltbf_proj` kernel and the HPM
assumption; **M6** the measured SINR stated without "unbiased", the scale-invariant SINR added, gate Q.
Minor: SRS amplitude 2 removed in `P`; DMRS op counts, band edges, delay removal, the boost; data
symbols before DMRS held; the systolic/vector `Kmax`/`Nmax` names; `CreditIn`; the gate-E equalizer
checked and priced; `J`, ring depth, Ctrl cycle counters, `w` storage, Cholesky, the shift-to-UE
assignment listed.
