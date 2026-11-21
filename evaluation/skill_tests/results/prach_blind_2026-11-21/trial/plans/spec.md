# PRACH searcher -- spec

What the accelerator computes, and for whom.  Every choice no one confirmed is marked **assumed**;
each has an entry in `../decisions.md`.

## 1. The configuration (a "reasonable config in a 20 MHz channel")

| item | value | source |
|---|---|---|
| carrier | 20 MHz, FR1 TDD, SCS 30 kHz, 51 PRBs | TS 38.101-1 Table 5.3.2-1 (51 PRBs at 20 MHz / 30 kHz) |
| sample rate | 30.72 Msps, 1024-point OFDM symbol | 2048 kappa 2^-mu Tc with kappa = 64, mu = 1 |
| PRACH | short preamble, L_RA = 139, Df_RA = 30 kHz, 12 PRBs | TS 38.211 Table 6.3.3.1-2, 6.3.3.2-1 (N_RB^RA = 12, k_bar = 2) |
| format | **B4** (12 repetitions + 468-sample CP) -- **assumed**; every short format (1 to 12 repetitions) accepted | TS 38.211 Table 6.3.3.1-2 |
| N_CS | **23** (zeroCorrelationZoneConfig 11): 6 shifts a root, 11 roots for 64 preambles -- **assumed** | TS 38.211 Table 6.3.3.1-7 |
| cell radius it covers | N_CS / (139 x 30 kHz) = 5.5 us zone; **usable round trip, delay spread included: the zone less ~1 us = 4.5 us, ~0.68 km** (the 0.52 us window margin, and a late UE's first sidelobe kept out of the next zone; decision 17).  N_CS 27 for a ~0.8 km cell | arithmetic; `results/margin_rel.json` |
| restricted sets | none (unrestricted set only) -- **assumed** | |
| preambles | 64 | TS 38.211 6.3.3.1 |
| receive antennas | **4** (the RFSoC 4x2 brings out four ADCs) -- **assumed** | `systems/library.md`, *The part* |
| msg1-FDM | 1 occasion per command; a second FDM occasion is a second command on the same windows | **assumed** |

Because a short-preamble symbol is exactly one 1024-sample OFDM symbol at 30.72 Msps, the 139 PRACH
subcarriers are 139 consecutive bins of a 1024-point FFT -- the reason the design needs no
resampling (`arch.md`).

## 2. Inputs and outputs

One command-response kernel (`docs/guide/systems/command_response.md`), all streams 128 bits:

| stream | carries | type, shape, precision | from / to |
|---|---|---|---|
| `s_in` | `PrachCmd(tx_id, n_ant, n_sym, k_start, n_cs, n_pre, n_roots, thr_q8)` | one word | the PRACH scheduler (software); `thr_q8` from the table `prach/thresholds.py` by `(n_ant, n_cs)`, Pfa 1e-3 (decision 18) |
| | the roots: `(u, u_inv)` x `n_roots`, U8 | 16 a word | software computes `u` (logical -> physical root, Table 6.3.3.1-4) and `u^-1 mod 139` |
| | `n_sym x n_ant` windows, symbol-major then antenna: 1024 complex int16 each (`ap_fixed<16,2>`), CP removed | 4 samples a word, 256 words a window | the capture front-end: AGC'd ADC samples cut at the occasion's FFT windows -- **assumed** to exist upstream |
| `m_out` | `Resp(tx_id, n_det, exp)` then `n_det` x `DetRec(preamble, n_peak, delay, peak U40, noise U48, n_noise U16)` | one word each | the MAC (random-access response) |
| `m_status` | `Status(tx_id, status)` one per command: OK, TLAST_EARLY, BAD_PARAM, BAD_ROOT, TLAST_LATE | one word | control |

Software turns a detection into a timing advance: `tau = ((n_peak 139/256 - (139 - C_v) mod 139) mod 139) / (139 x 30 kHz)`
(`golden.delay_estimate_s`), `T_A = round(tau / (16 x 64 Tc / 2))`.

## 3. The operation

Defined by `prach/golden.py` (module docstring, steps 1 to 6), bit-exact (`search_fixed`) and in
float64 (`search_float`):

1. FFT-1024 of every window (the library `SsrFft`'s bit-exact model);
2. the 139 bins from `k_start` (mod 1024) summed over the repetitions (coherent), then a block exponent
   `e` per occasion: `Y = sat16(rnd(A / 2^e))`;
3. per root, `Z = sat16(rnd(conj(Y) R_u / 2^16))`, `R_u(k) = exp(+j pi u m(m+1)/139)`, `m = u^-1 k mod 139`
   (the root's DFT up to a constant phase), zero-padded to 256;
4. FFT-256 of `Z` (the cyclic cross-correlation, 130 ns a sample);
5. the power-delay profile `P[n] = sum_a |sat18(rnd(z / 2^6))|^2`;
6. per preamble, the peak over its zone's window (4 samples of margin before its zero-delay point);
   the noise `S` = the profile's sum less +-2 samples round every window's peak, `N` its sample
   count; detected iff `peak x N x 2^8 > thr_q8 x S` (peak over that mean > `thr_q8 / 256`) **and**
   `40 x peak >` the root's strongest window peak (above -16 dB of it: not a stronger UE's sidelobe).
   Decision 17: near-far ~15 dB on one root (10 dB with the whole-profile mean), no sidelobe ghosts
   inside the usable zone.

**Edge cases.**  `n_cs = 0` -> one preamble a root, the whole profile its window.  A zone wraps round
the profile (mod 256).  PRACH bins wrap round bin 1023 (`k_start` near 1024).  A root whose shifts
outnumber the remaining preambles searches only `p < n_pre`.  No detections -> `Resp(n_det = 0)` and no
record burst.  Errors are fatal (`m_status` carries which): a header field out of range or an
`n_roots` that disagrees with `n_cs` / `n_pre` (BAD_PARAM); a root with `u` outside 1..138 or
`u u_inv != 1 mod 139` (BAD_ROOT); roots or a window ending early (TLAST_EARLY); the command, the
roots or a window with no TLAST on its last word -- a burst longer than announced (TLAST_LATE,
decision 20: without it front would read on into the next burst and answer every later job `OK`
on misaligned data).  Saturation: `Y` cannot
saturate (the block exponent), `Z` and `q` saturate at bounds the signal cannot reach (|Z| <= 2^14.5,
|q| <= 2^16.2, both derived in `golden.py`); the clamps remain as guards.

**Golden.**  `python -m pytest tests -q -k golden` runs the worked example (three UEs, found at their
own preambles, delays within one TA step, fixed against float within 1 %).  `python scratch/summary.py`
prints every scenario's expected detections.

## 4. Why hardware, as numbers

Per B4 occasion, 4 antennas, 11 roots (counts from the steps above; radix-4 FFT: `3L/4` twiddle products
per stage boundary):

| step | complex multiplies | share |
|---|---|---|
| FFT-1024, 48 windows | 48 x 4 x 768 = 147,456 | 80 % |
| FFT-256, 44 frames | 44 x 3 x 192 = 25,344 | 14 % |
| correlate, 44 x 139 | 6,116 | 3 % |
| power, 44 x 256 (2 real products = 1/2 CM) | 5,632 | 3 % |
| **total** | **~184,500** | |

At the workload's worst case (one occasion a slot, 0.5 ms): **369 M complex multiplies/s = 1.5 G real MAC/s**.

**CPU baseline** (`CpuPlatform.sw_function("cdot_q15")`, A53 at 1.2 GHz, gem5-calibrated; mapping: a
complex multiply counted as four `cdot_q15` elements, adds ignored -- generous to the CPU): 736,000
elements an occasion = **6.7 ms on one core** priced as 180 calls of n = 4096 (11.0 ms as one call of
n = 736k -- outside any size I know the family was calibrated at).  Against 0.5 ms: **13x short on one
core, 3.4x short on all four**, before the A53 does anything else.  The hardware below does it in 49 us
(`eval.md`): ~140x one core.

## 5. Requirements

| requirement | value | assumption |
|---|---|---|
| rate | one B4 occasion, 4 antennas, 64 preambles, every 0.5 ms slot | worst-case PRACH configuration: an occasion in every UL slot -- **assumed** |
| latency | detections within 0.5 ms of the occasion's last sample | msg2 (RAR) is scheduled no earlier than the next slots; **assumed** |
| detection | Pfa <= 1e-3 per occasion on noise; Pd >= 99 % at the operating SNR | TS 38.141-1 8.4 uses Pfa 0.1 % and Pd 99 %; its SNR points for B4 are not reproduced here -- check them |
| timing estimate | within one TA step, 16 x 64 Tc / 2 = 0.26 us | TS 38.213 4.2 (TA granularity at 30 kHz) |
| part, clock | xczu48dr (RFSoC 4x2), 250 MHz, Vitis 2025.1 | the platform every library model is filed on |

## 6. Workload

| parameter | range | typical | how often |
|---|---|---|---|
| `n_sym` | 1..12 | 12 (B4) | every command |
| `n_ant` | 1..4 | 4 | every command |
| `n_roots` | 1..64 | 11 (N_CS 23); 64 only with N_CS = 0 | config changes rarely |
| detections | 0..64 | 0 most occasions, 1-3 when busy | |
| errors | never in operation (a sender bug) | | |

Cost scales with `n_sym x n_ant` (input) and `n_roots x n_ant` (correlation); the worst case for the
back end is N_CS = 0 (64 roots).

## Open questions (most important first)

1. **Format and N_CS** (B4, 23): the cell size and coverage target decide them; both are command
   fields, so this changes the workload, not the hardware.
2. **The capture front-end** that cuts the windows (CP removal, symbol-major, antenna interleave) is
   assumed upstream; on the RFSoC it is an `RfShotRx`-like capture plus a sequencer -- not designed here.
3. **Restricted sets** (high speed) are not supported.
4. **The conformance SNR points** of TS 38.141-1 for B4 should be checked against `results/perf_*.json`.
5. **Near-far beyond ~16 dB on one root** is not detected (the relative rule, decision 17); closed-loop
   power ramping usually keeps PRACH arrivals within that, but a cell that needs more needs
   interference cancellation (subtract the strong UE, search again) -- a second pass not designed here.
6. **Residual CFO** above ~1 kHz loses the 12-repetition coherent sum (Pd 0.98 at 1 kHz, 0.29 at
   1.5 kHz, -20 dB; `results/robust.json`) -- about 0.35 kHz of UE error plus twice the Doppler, i.e.
   ~100 km/h at 3.5 GHz (arithmetic, decision 19); a high-speed or poorly-locked UE needs non-coherent
   combining of repetition groups -- a change to extract, not designed here.
7. The logical -> physical root mapping (`nr.logical_to_physical_root`) is from the table's pattern;
   verify against Table 6.3.3.1-4.  The hardware takes physical `u`.
