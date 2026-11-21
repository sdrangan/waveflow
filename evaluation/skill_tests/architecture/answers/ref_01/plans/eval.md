# eval.md -- the MMSE MIMO detector: does it fit

Every number here is printed by `python price.py` (from the repository root), which names its source:
**[std]** the 3GPP tables, **[cpu]** the A53 platform's models, **[rm]** a resource model on the packaged
platform `xczu48dr_250mhz_vitis2025_1` (with the confidence it reports), **[bound]** the systolic
engine's bounds (`dsp/systolic/systolic_theory.md`) plus the +7 % measured at its centre (171 cycles
against 160, Vitis 2025.1), **[meas]** an RTL point on a guide page (Vitis 2025.1), **[asm]** an
assumption from `spec.md`. No simulation; nothing synthesized.

The rate: **457.9 cycles a filter group** (one PRB, one slot) at 250 MHz, 546,000 groups a second
[std]. The part: 4,272 DSP, 425,280 LUT (AMD's product table); above ~70 % of LUT is a timing risk at
250 MHz (a rule of thumb, `systems/library.md`).

## The operations, weighted by the workload

| term | a second, `U = 8` | share | what binds |
|---|---|---|---|
| apply `W y` | 23.48 G CMAC | 78-91 % | **the input**: 11.7 antenna samples a cycle, 2.94 128-bit words a cycle |
| compute `W`, direct | 2.28 G CMAC | ~9 % | the solve's serial dependences (square roots, divides) |
| compute `W`, CG at 5 iterations | 6.71 G CMAC | ~22 % | the vector unit's execute task [meas] |

**The bottleneck is moving `Y`, not inverting `G`.** The paper spends its pages on the inverse; the
hardware spends its cycles streaming 32 antennas' samples. Because the apply is input-bound, it costs
the same at 2 users as at 8: the workload's "mostly 2-4 users" saves nothing there.

## The shared apply path (A and B)

`SystolicEngine`, `R 4, C 8, L 4`, `Kmax 32, Pmax 1, Nmax 32`, 128-bit words, `keep_a`; per filter group
one command of 7 outputs `8 × 24`:

| per output | cycles | source |
|---|---|---|
| compute bound `T·P` (6 tiles × 32) | 192 | [bound] |
| input bound (`B` only, `keep_a`) | 192 | [bound] -- reading `keep_a`'s input as `k·n/LW` is my inference from the page |
| output bound | 48 | [bound] |
| **a filter group** (7 outputs + `W` once + ~30 a command, +7 %) | **1,537** | [bound] [meas centre] |

| | value | source |
|---|---|---|
| engines at the rate | 3.36 → **4** (84 % busy each) | count |
| 4 engines | **616 DSP, 149,204 LUT, 178,096 FF, 224 BRAM18** | [rm] **UNCALIBRATED** as reported; `Kmax 32` is outside the model's sweep (`Kmax` 4-16) |
| latency, a filter group | ~1,537 cycles (6 µs) | [bound]: far inside one slot |

`dist` and `coll` (built): a word a cycle each, II = 1 by the lane loop (`vectorization/hls/loop_optimization.md`);
resources **unknown -- csynth probe** (expected small: no arithmetic).

## Alternative A -- CG on the vector unit

| block | per filter group | source |
|---|---|---|
| `VecUnit` L 4, CG `8 × 32`, 5 iterations | 5,455 cycles | [meas] 1,439 / 3,447 / 8,467 at 1 / 3 / 8 iterations, linear between |
| `VecUnit` L 8, the same | 3,467 cycles | [meas] 2,191 at 3 iterations, scaled as L 4 scales [asm] |
| `gram_cg_mm`, Gram + 5 products `8×8×32` | 639 cycles | [bound] Gram, [meas] 94 cycles a centre product at 128 bits |
| `gram_cg_mm` resources | 154 DSP, 37,690 LUT | [rm] UNCALIBRATED |

Units needed, by how often `W` is computed (the channel granularity, `spec.md` open question 1):

| `W` every | budget a solve | units | LUT (units) | **total LUT with the apply and `gram_cg_mm`** | fits? |
|---|---|---|---|---|---|
| 1 PRB | 458 | 12 × L4 | 978,372 [rm] INTERPOLATED | 1.17 M (274 %) | **no** |
| 4 PRB | 1,831 | 3 × L4 | 244,593 [rm] | 431,487 (**101 %**) | **no** |
| 8 PRB | 3,663 | 1 × L8 | 137,287 [rm] | 324,181 (76 %) | over 70 %: a timing risk |

**A does not fit at the estimator's finest granularity**, and only fits at 8 PRBs a filter (2.9 MHz
of flat channel), with little room. Back to `arch.md`: that is what B is for.

## Alternative B -- a direct solve, built

| block | per filter group | source |
|---|---|---|
| `gram`, `8 × 32 × 8` | 169 cycles (37 % of 458): one engine | [bound] |
| `gram` resources | 154 DSP, 37,690 LUT | [rm] UNCALIBRATED |
| `chol_solve`: the triangular solves, 32 RHS × 16 row steps, one `U`-lane dot a step at II = 1 | 512 cycles → **2 instances** at 458 | lane loop [asm: II = 1 holds] |
| `chol_solve`: the factorization, 8 square roots and 36 divides, serial | **unknown -- csynth probe** | no model covers it |
| `chol_solve` resources | **unknown -- csynth probe**; at least `U` complex multipliers a lane loop (4 DSP each in the 4-multiply form, `dsp/systolic/systolic_engine.md`) | |
| **total LUT, known part** | 149,204 + 37,690 = 186,894 (44 %), plus `chol_solve`, `dist`, `coll` | [rm] |

**The probe that settles B**: csynth (Vitis 2025.1) of one `chol_solve` at `U = 8`, `B = 32`, 16-bit
`H`, with its II over the right-hand sides and its latency for the factorization. B fits if two
instances cost under ~110k LUT (keeping the total under 70 %) and the factorization overlaps the
previous group's solves.

## Alternative C -- a solve per resource element

333 CMAC an RE (256 + 64, and 13 for its Gram and factor amortized over 168 REs) against B's 256 + 25
(`W` amortized): **18 % more work on the rate-critical path**, and two serial triangular solves per RE at 0.37 REs a cycle. Rejected
on the count; not priced further.

## Recommendation

**B**, after its probe: it is exact (no iteration count to defend), fits at the estimator's finest
granularity, and leaves ~55 % of the LUTs for the probe to spend. **A** is the fallback that needs no
new block -- every part is a library block with a model -- if the estimator's team confirms a filter
per 8 PRBs and you accept 76 % LUT.

## Open questions

1. **You pick: B (probe first) or A (if the granularity is 8 PRBs).**
2. The apply engines' LUT and FF are extrapolated (`Kmax 32`): one csynth of one engine settles them.
3. Both alternatives need four `Y` streams in (2.94 words a cycle): can the FFT provide them?
