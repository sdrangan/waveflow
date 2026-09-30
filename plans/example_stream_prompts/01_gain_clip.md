# Accelerator spec: `gain_clip`, gain, round, clip and count

**Read [frame.md](frame.md) first.** It defines the protocol, errors, flow,
stages and report. This file adds only what is specific to `gain_clip`.

## 1. Overview

Scale a block of signed 16-bit samples by a programmable gain, round, clip
the result to a programmable range `[lo, hi]`, and report per transaction how
many samples were clipped at each end. All arithmetic is integer, and every
output must be **bit-exact** to the oracle.

## 2. Register map parameters

These are written by the host before `ap_start`, with access RW:

| Field | Type | Meaning |
| --- | --- | --- |
| `gain` | int16 | gain `g` in **Q8.8**: the real gain is `g / 256` |
| `lo` | int16 | lower clip bound |
| `hi` | int16 | upper clip bound |

**`BAD_PARAM`** is raised when `lo > hi`.

## 3. Data

- **Input samples `x`:** int16, packed two per 32-bit word by the Waveflow
  array utilities. `layout.md` must state which half holds the even sample,
  and what fills the unused half when `nsamp` is odd.
- **Output samples `y`:** int16, packed the same way.
- **Response footer, after `nsamp_read`:**

  | Field | Type | Meaning |
  | --- | --- | --- |
  | `n_clip_lo` | uint16 | processed samples with `r < lo` |
  | `n_clip_hi` | uint16 | processed samples with `r > hi` |

## 4. Exact function

```
p = x * g                          # exact product; |p| <= 2^30
r = floor((p + 128) / 256)         # i.e. (p + 128) >> 8, arithmetic shift; r needs >= 24 signed bits
y = min(max(r, lo), hi)
n_clip_lo += (r < lo)
n_clip_hi += (r > hi)
```

This is **round half up**, meaning ties go toward +infinity. It is *not*
round-half-away-from-zero and it is *not* truncation. A sample with
`r == lo` or `r == hi` exactly is **not** counted as clipped.

Worked examples (all must hold exactly). "full" means `lo = -32768, hi = 32767`.

| x | g | p | r | lo, hi | y | counts |
| --- | --- | --- | --- | --- | --- | --- |
| 3 | 128 | 384 | 2 | full | 2 | none |
| -3 | 128 | -384 | -1 | full | -1 | none |
| 1 | 128 | 128 | 1 | full | 1 | none |
| -1 | 128 | -128 | 0 | full | 0 | none |
| 32767 | 512 | 16776704 | 65534 | full | 32767 | n_clip_hi += 1 |
| -32768 | -32768 | 1073741824 | 4194304 | full | 32767 | n_clip_hi += 1 |
| 100 | 256 | 25600 | 100 | -50, 50 | 50 | n_clip_hi += 1 |

## 5. Scenarios

Each scenario is one kernel run: register writes, then the listed commands.
Unless the scenario halts on an error, the run ends with `END`.

| ID | Registers | Commands | Purpose |
| --- | --- | --- | --- |
| S1_random | g=384 (1.5), lo=-20000, hi=20000 | DATA nsamp=1000, x uniform over int16 | general case, clipping both ways |
| S2_ties | g=128, full | DATA: x = every odd value in [-255, 255] | every product is a rounding tie |
| S3_extremes | g=-32768, full; then a second run with g=32767 | DATA: x over {-32768, -1, 0, 1, 32767} | overflow of p and r |
| S4_identity | g=256, full | DATA nsamp=500 random | y == x, counts 0 |
| S5_lo_eq_hi | g=256, lo=hi=7 | DATA nsamp=100 random | every output is 7 |
| S6_multi | g=384, lo=-20000, hi=20000 | DATA nsamp=1, DATA nsamp=7, DATA nsamp=0, DATA nsamp=200 | odd-count padding, empty transaction, per-transaction footers |
| E1_bad_param | lo=10, hi=-10 | DATA nsamp=10 | halts BAD_PARAM with tx_id, emits nothing |
| E2_early_tlast | full | DATA nsamp=10 with TLAST on sample word 2 | halts code 3; 6 samples and footer emitted |
| E3_no_tlast | full | DATA nsamp=10 with no TLAST on the last word | halts code 4; 10 samples and footer emitted |
| S_TIMING | g=384, full | DATA nsamp=4096 random | timing only |

## 6. Acceptance

**Functional:** frame F7, all three comparisons, bit-exact on every output
word and every TLAST flag, and the register-map status equal to the oracle's.

**Timing** (cosim of S_TIMING, measured from the VCD):

- `T_total` is the number of cycles from the handshake (TVALID && TREADY)
  of the first command-header word to the handshake of the last footer word.
  Required: `T_total <= 2048 + 60`, where 2048 = `4096 / 2` sample words.
- C-synthesis shows the sample loop at **II = 1 per 32-bit word** (two
  samples per clock).
- pysim vs cosim cycle count within 20 cycles (F7).

**Implementation** (C-synthesis report): estimated clock `<= 7.3 ns`,
DSP `<= 4`, BRAM_18K `<= 2`, LUT `<= 3000`, FF `<= 4000`.
