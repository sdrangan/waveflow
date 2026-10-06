# Accelerator spec: `fir`, a streaming FIR filter with up to 16 taps

**Read [frame.md](../frame.md) first.** It defines the protocol, errors, flow,
stages and report. This file adds only what is specific to `fir`.

## 1. Overview

Filter blocks of signed 16-bit samples with a programmable FIR filter of 1 to
16 taps. The filter history is **cleared at the start of every `DATA`
transaction**. The kernel must keep up with the stream at two samples per
clock, and it must **stream**: output starts long before the input burst
ends. All arithmetic is integer, and every output must be **bit-exact** to
the oracle.

## 2. Command-header parameters

These travel in every `DATA` command header, after `cmd_type`, `tx_id` and
`nsamp` (frame F3), so each command carries its own. Nothing is written over
AXI-Lite but `ap_start`:

| Field | Type | Meaning |
| --- | --- | --- |
| `ntaps` | uint8 | number of active taps, 1 to 16 |
| `taps` | int16[16] (`DataArray`) | `h[0..15]` in Q1.15; only `h[0..ntaps-1]` are used |

**`BAD_PARAM`** is raised when `ntaps == 0` or `ntaps > 16`. Taps `h[k]`
with `k >= ntaps` must be **ignored even if they are nonzero**.

## 3. Data

- **Input samples `x`:** int16, packed two per 32-bit word by the Waveflow
  array utilities. `layout.md` must state which half holds the even sample,
  and what fills the unused half when `nsamp` is odd.
- **Output samples `y`:** int16, packed the same way.
- **Response footer**, after the data burst, sent only when the command
  succeeds (frame F3):

  | Field | Type | Meaning |
  | --- | --- | --- |
  | `n_sat` | uint16 | processed samples where `r` was outside int16 |

## 4. Exact function

Index one transaction's samples `x[0..nsamp-1]`, and take `x[n] = 0` for
`n < 0` in **every** transaction. For each `n`:

```
acc[n] = sum_{k=0}^{ntaps-1} h[k] * x[n-k]    # exact; >= 36 signed bits; no rounding of partial sums
r[n]   = floor((acc[n] + 16384) / 32768)     # (acc + 2^14) >> 15, arithmetic shift
y[n]   = min(max(r[n], -32768), 32767)
n_sat += (r[n] > 32767) or (r[n] < -32768)
```

Worked examples (all must hold exactly):

| Case | Result |
| --- | --- |
| ntaps=1, h[0]=32767, x[0]=32767 | acc=1073676289, r=32766, y=32766 (not 32767) |
| ntaps=1, h[0]=-32768, x[0]=-32768 | acc=2^30, r=32768, y=32767, n_sat=1 |
| acc=16384 | r=1 |
| acc=-16384 | r=0 |
| impulse x=[16384, 0, 0, ...], any taps | y[k] = floor((h[k]+1)/2) for k < ntaps, then 0 |

## 5. Scenarios

Each scenario is one kernel run: the listed commands, each `DATA` header
carrying the parameters shown.
Unless the scenario halts on an error, the run ends with `END`.

| ID | Parameters | Commands | Purpose |
| --- | --- | --- | --- |
| S1_impulse | ntaps=16, random taps | DATA x=[16384, 0 x 19] | output is the halved taps: checks tap order |
| S2_random | ntaps=16, random taps with sum of \|h\| <= 32767 | DATA nsamp=1000, x uniform over int16 | general case, n_sat = 0 |
| S3_saturate | ntaps=16, \|h\| = 32767 | DATA nsamp=200, x = +/-32767 matching the tap signs | saturation both ways |
| S4_ignored_taps | ntaps=4, h[4..15] = 32767 | DATA nsamp=100 random | equals the same run with h[4..15] = 0 |
| S5_short | ntaps=16 | DATA nsamp=3 | fewer samples than taps |
| S6_history_reset | ntaps=16, random taps | DATA nsamp=1000 random, then DATA impulse as in S1 | a leaked history corrupts the impulse response |
| S7_multi | ntaps=5 | DATA nsamp=1, DATA nsamp=7, DATA nsamp=0 | odd-count padding, empty transaction |
| S8_near_identity | ntaps=1, h[0]=32767 | DATA x over {-32768, -32767, -1, 0, 1, 32766, 32767} | the 1-LSB loss of Q1.15 "unity" |
| E1_ntaps_0 | ntaps=0 | DATA nsamp=10 | halts BAD_PARAM, emits nothing |
| E2_ntaps_17 | ntaps=17 | DATA nsamp=10 | halts BAD_PARAM, emits nothing |
| E3_early_tlast | ntaps=4 | DATA nsamp=10 with TLAST on sample word 2 | halts code 1; 6 samples emitted, the last with TLAST; no footer |
| S_TIMING | ntaps=16, random taps | DATA nsamp=4096 random | timing only |

## 6. Acceptance

**Functional:** frame F7, all three comparisons, bit-exact on every output
word and every TLAST flag, and the status registers equal to the oracle's.

**Timing** (cosim of S_TIMING, measured from the VCD):

- `T_total` is the number of cycles from the handshake (TVALID && TREADY)
  of the first command-header word to the handshake of the last footer word.
  Required: `T_total <= 2048 + 90` (the header carries `ntaps` and the 16
  taps, about 9 more words than a header without parameters).
- `L_first` is the number of cycles from the handshake of the first
  **sample** word in to the handshake of the first **data** word out.
  Required: `L_first <= 40`. This is the streaming requirement.
- C-synthesis shows the sample loop at **II = 1 per 32-bit word for
  ntaps = 16**. Full rate is required at the maximum tap count.
- pysim vs cosim cycle count within 20 cycles (F7).

**Implementation** (C-synthesis report): estimated clock `<= 7.3 ns`,
DSP `<= 40`, BRAM_18K `<= 2`, LUT `<= 6000`, FF `<= 8000`.

**Report addition:** describe in a few sentences how the two lanes and 16
taps map onto hardware.
