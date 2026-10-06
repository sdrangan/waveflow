# Accelerator spec: `cmag_peak`, complex magnitude and peak search

**Read [frame.md](../frame.md) first.** It defines the protocol, errors, flow,
stages and report. This file adds only what is specific to `cmag_peak`.

## 1. Overview

For each block of complex single-precision samples, output the magnitude
`|z|` of every sample, and report per transaction where the largest magnitude
occurred and how many samples exceeded a threshold. The arithmetic is
**floating point**, so outputs are *not* bit-exact to the oracle. Section 6
defines exactly what counts as correct, and that definition is part of the
design.

## 2. Command-header parameters

This travels in every `DATA` command header, after `cmd_type`, `tx_id` and
`nsamp` (frame F3), so each command carries its own. Nothing is written over
AXI-Lite but `ap_start`:

| Field | Type | Meaning |
| --- | --- | --- |
| `thresh` | float32 | detection threshold on `\|z\|` |

**`BAD_PARAM`** is raised when `thresh` is NaN, +/-infinity, or `< 0`.
`-0.0` is legal and equals 0.

## 3. Data

- **Input sample `z = I + jQ`:** two float32 words, `I` then `Q`. Define it
  as a Waveflow `DataList` with fields `i` and `q`, and confirm the order in
  `layout.md`. A burst has `2 * nsamp` words. Under frame F4's code 1, a
  half-received sample (an `I` without its `Q`) is discarded.
- **Input range:** every `I` and `Q` is either `+/-0.0` or satisfies
  `1e-15 <= |v| <= 1e15`. Within this range `I*I + Q*Q` neither overflows
  nor goes subnormal in float32. Behavior outside it is unspecified and
  untested.
- **Output `m`:** one float32 word per processed sample.
- **Response footer**, after the data burst, sent only when the command
  succeeds (frame F3):

  | Field | Type | Meaning |
  | --- | --- | --- |
  | `peak_idx` | uint16 | **first** index of the largest `m[k]`; `0xFFFF` if `nsamp = 0` |
  | `peak_mag` | float32 | `m[peak_idx]`; `+0.0` if `nsamp = 0` |
  | `n_above` | uint16 | processed samples with `m[k] > thresh` (strict) |

  The footer is computed from the kernel's **own** outputs `m[k]`.

## 4. Function

The kernel computes `m[k] = sqrt(I[k]^2 + Q[k]^2)` in float32, by any method
that meets section 6. The oracle computes
`m_ref[k] = hypot(float64(I[k]), float64(Q[k]))`.

| I | Q | m (required) |
| --- | --- | --- |
| 3.0 | 4.0 | 5.0 (within tolerance; exact in practice) |
| -3.0 | -4.0 | 5.0 |
| 0.0 | 0.0 | exactly `+0.0` (bits `0x00000000`) |
| -0.0 | -0.0 | exactly `+0.0` |
| 1e15 | 1e15 | about 1.41421356e15 |
| 1e-15 | 0.0 | about 1e-15 |

## 5. Scenarios

Each scenario is one kernel run: the listed commands, each `DATA` header
carrying the parameters shown.
Unless the scenario halts on an error, the run ends with `END`.

| ID | Parameters | Commands | Purpose |
| --- | --- | --- | --- |
| S1_random | thresh=2.0 | DATA nsamp=1000, I and Q ~ N(0,1) | general case |
| S2_exact | thresh=5.0 | DATA: the section 4 table | exact cases, signed zeros, a sample exactly at the threshold |
| S3_dyn_range | thresh=1.0 | DATA nsamp=1000, \|z\| log-uniform over [1e-15, 1e15], about 10% of components +/-0.0 | full input range |
| S4_planted_peak | thresh=2.0 | DATA nsamp=1000 N(0,1), sample 617 scaled by 100 | peak_idx = 617 |
| S5_ties | thresh=2.0 | DATA with an identical largest sample at 10 and 20; DATA with two maxima whose m_ref differ by < 1 ulp | exact tie means first index; near-tie allows either |
| S6_thresh_edge | thresh=3.0 | DATA: 50 samples within 2 ulp of thresh, 50 clearly above, 50 clearly below | n_above bounded, not fixed |
| S7_zero_thresh | thresh=-0.0 | DATA with some all-zero samples, DATA nsamp=0, DATA nsamp=1 | zeros are not counted; -0.0 is legal; empty footer |
| E1_neg_thresh | thresh=-1.0 | DATA nsamp=10 | halts BAD_PARAM |
| E2_nan_thresh | thresh=NaN | DATA nsamp=10 | halts BAD_PARAM |
| E3_inf_thresh | thresh=+inf | DATA nsamp=10 | halts BAD_PARAM |
| E4_early_mid | thresh=2.0 | DATA nsamp=10, TLAST on word 4 (an `I`) | halts code 1; 2 outputs emitted, the last with TLAST; no footer |
| E5_early_end | thresh=2.0 | DATA nsamp=10, TLAST on word 5 (a `Q`) | halts code 1; 3 outputs emitted, the last with TLAST; no footer |
| S_TIMING | thresh=2.0 | DATA nsamp=2048 N(0,1) | timing only |

## 6. Acceptance

Let `tol = 4 * 2^-24`, which is about 2.4e-7.

**Exact:** response headers, `nsamp`, every TLAST flag, the data-word
count and the status registers all match the oracle exactly.

**Data words:** if `m_ref[k] = 0`, then `m[k]` is exactly `+0.0`.
Otherwise `|m[k] - m_ref[k]| <= tol * m_ref[k]`.

**Footer.** Each statistic must be *consistent with the kernel's own data
burst* and *correct up to tolerance*:

- `peak_idx` is the first index of the maximum of the kernel's `m`, *and*
  `m_ref[peak_idx] >= (1 - 2*tol) * max m_ref`.
- `peak_mag == m[peak_idx]` bit-exactly.
- `n_above` equals the count of the kernel's `m[k] > thresh`, *and*
  `n_strict <= n_above <= n_loose`. Here
  `n_strict = #{m_ref > thresh*(1+tol)}` and
  `n_loose = #{m_ref > thresh*(1-tol)}`.

**Between simulations:** pysim, csim and cosim outputs need **not** be
bit-identical to each other, because the Python and C++ floating-point paths
may differ. Each one must pass the checks above against the oracle, and csim
and cosim must be bit-identical to each other. Report the worst relative
error of each, in ulp.

**Timing** (cosim of S_TIMING, measured from the VCD):

- `T_total` is the number of cycles from the handshake (TVALID && TREADY)
  of the first command-header word to the handshake of the last footer word.
  Required: `T_total <= 4096 + 100`.
- `L_first` is the number of cycles from the handshake of the first sample
  word in to the handshake of the first data word out. Required:
  `L_first <= 80`.
- The kernel accepts one input word per clock, which is II <= 2 per sample.
- pysim vs cosim cycle count within 20 cycles (F7).

**Implementation** (C-synthesis report): estimated clock `<= 7.3 ns`,
DSP `<= 16`, BRAM_18K `<= 2`, LUT `<= 8000`, FF `<= 10000`.

**Stage 1 addition:** `check.py` must reject these three mutants:

- a data word off by 16 ulp;
- `peak_idx` set to the last tied index in S5;
- `n_above` computed with `>=` in S2.
