# spec.md -- the MMSE MIMO detector: what

Status: **checkpoint 1**. The operation, the requirements and the workload below are proposed; the
items marked **assumed** need your confirmation before `eval.md` is trusted.

## Inputs and outputs

| signal | what | shape | precision | from / to |
|---|---|---|---|---|
| `Y` | frequency-domain samples, one filter group's resource elements | `B × n` complex, `B = 32`, `n = 168` (12 subcarriers × 14 symbols) | 16-bit I and Q, `ap_fixed<16,1>` (**assumed**: the FFT's output scaled to `|re|, |im| < 1`) | the FFT (not ours) |
| `H` | the channel estimate for one PRB and slot | `B × U` complex, `U ≤ 8` | 16-bit I and Q, `ap_fixed<16,1>` (**assumed**, the same scale as `Y`) | the channel estimator (not ours) |
| `sigma2` | the noise variance for that PRB and slot | a real scalar, `> 0` | 16 bits, unsigned (**assumed**) | the channel estimator |
| `x_hat` | the detected symbols | `U × n` complex | 16-bit I and Q, `ap_fixed<16,3>`: unit-energy QAM, 256-QAM's corner at 1.53, headroom to 4 | the demapper (not ours) |

## The operation

```text
G = H^H H + sigma2 I            (U x U, Hermitian positive definite since sigma2 > 0)
W = G^{-1} H^H                  (U x B), once per filter group (one PRB, one slot)
x_hat = W y                     for every resource element y of the group
```

Executable definition: `golden.py` (`mmse_filter`, `detect`; `python golden.py` prints the checks
below). Edge cases, all in `golden.py`:

- `U = 0` (no user on the PRB): no output, no work.
- `sigma2 <= 0` or not finite: refused. Without the noise term `H^H H` can be singular; that is
  zero-forcing, a different operation.
- `U > B` or `U > 8`: refused (more layers than antennas, or than the design is sized for).
- A saturated output (`|x_hat|` beyond 4): saturates, as `ap_fixed<.., AP_SAT>` does; the demapper
  sees the clipped value.
- `W` is never inverted directly in hardware: "the inverse" is a solve of `G W = H^H` (alternatives
  in `arch.md`).

**Formats, checked.** On the scenario of `golden.scenario` (i.i.d. Rayleigh, QPSK, 20 dB SNR, 20
seeds): quantizing `H`, `Y`, `W` and `x_hat` to the formats above costs **-78.6 dB** (worst, 8 users)
against the floating-point golden -- far below the noise at -20 dB. `|W|` reached 1.19 at 8 users, so
`W` is `ap_fixed<16,2>`, not `<16,1>` (the first proposal saturated: -45 dB). The direct solve's own
fixed-point arithmetic is not yet modelled (open question 3).

**CG accuracy** (alternative A's solve, `golden.cg_filter`): `U` iterations are exact; at `U = 8`,
3 iterations give -19.9 dB worst against the direct solve (as large as the noise), 4 give -27.2 dB,
5 give **-38.1 dB**. So A needs **5 iterations at 8 users** (**assumed** adequate; a link-level BER
curve would settle it).

## Why hardware, as numbers

| quantity | value | source |
|---|---|---|
| resource elements a second | 91.73 M | 273 PRB × 12 × 14 × 2,000 slots/s (below) |
| `W y` complex MACs a second at `U = 8` | 23.48 G | `RE/s × U × B` |
| computing `W` (Gram + Cholesky + two triangular solves) | 2.28 G CMAC/s | 4,181 CMAC a PRB-slot × 546 k/s |
| A53 cores to apply `W` at the rate | **859** | `cdot_q15` model, 11,242 cycles for one RE's 1,024 real MACs, at 1.2 GHz (`CpuPlatform`, a53 HPI gem5 v25.1) -- a complex MAC counted as four `cdot_q15` elements |
| A53 cores to compute `W` | 87 | the same model; square roots and divides not counted (optimistic) |

The RFSoC has four A53 cores: the detector is two to three orders of magnitude beyond software. The
CPU keeps the per-slot control (who is scheduled where): 1,281 cycles a slot for 69 commands
(`dispatch` model), 0.2 % of one core.

## Requirements

| requirement | value | source |
|---|---|---|
| carrier | 100 MHz, FR1, **30 kHz** subcarrier spacing (**assumed**: 15 kHz cannot carry 100 MHz; 60 kHz is allowed but rare) | the user; TS 38.101-1 Table 5.3.2-1 |
| PRBs | 273 (3,276 subcarriers) | TS 38.101-1 Table 5.3.2-1, 100 MHz at 30 kHz |
| symbols | 14 a slot, 2,000 slots a second (0.5 ms slots) | TS 38.211, normal cyclic prefix, μ = 1 |
| detected symbols | all 14 of every slot (**assumed** worst case: DMRS symbols carry no data, so 12 or 13 is the real number; sizing for 14 gives 8-15 % margin) | |
| antennas `B` | 32 | the user |
| users `U` | up to 8 at once; **the design must meet the rate at 8** | the user |
| channel granularity | one `H` and `sigma2` per PRB per slot (**assumed**: the estimator's finest; coarser is cheaper, see `eval.md`) | to agree with the estimator's team |
| latency | the detector adds at most **one slot** (0.5 ms) after a slot's last sample (**assumed**: the uplink HARQ budget is the whole receiver's; the detector should take a small part) | |
| part and clock | `xczu48dr` (RFSoC 4x2) at 250 MHz, Vitis 2025.1 | the user's board; the packaged platform's clock |
| `Y` arrives | on chip, as streams from the FFT (**assumed**; through DDR it would need 94 Gb/s in, which no calibrated bus model here covers) | |

## Workload

| what | value | source |
|---|---|---|
| users a PRB | 1 to 8; mostly 2 to 4 | the user |
| PRBs in use | all 273 in a busy slot (**assumed** worst case) | |
| filter groups a second | 546,000 (273 × 2,000) | above |
| work that depends on `U` | computing `W` (∝ `U²B`, `U³`) and the output; **not** the input (`B` samples an RE whatever `U`) | count |

Sizing for 8 users costs little in the apply (input-bound: `eval.md`); it costs in computing `W`,
which at 2-4 users would be 4 to 16 times cheaper.

## Open questions

1. **The channel granularity**: does the estimator deliver `H` per PRB per slot, or per group of 4 or
   8 PRBs? It decides whether alternative A fits (`eval.md`).
2. Is 5 CG iterations at 8 users acceptable, given a BER curve? (Only alternative A.)
3. The fixed-point arithmetic of the direct solve (alternative B): its formats need their own golden.
4. Confirm 30 kHz SCS, sizing for 14 symbols, and one slot of latency.
5. Does `Y` reach us on chip from the FFT, or through DDR?
6. Does the demapper need each user's post-detection SINR (the MMSE bias) beside `x_hat`?
