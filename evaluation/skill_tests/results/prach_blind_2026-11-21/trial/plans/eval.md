# PRACH searcher -- does it fit

No simulation in the pricing below except where it says so; every number names its source.  The
workload is `spec.md`'s worst case: a B4 occasion, 4 antennas, 11 roots, every 0.5 ms = **125,000
cycles at 250 MHz**.  csynth has not been run (Vitis HLS is not installed here): every resource of a
built stage is **unknown -- needs a csynth probe**, with the probe named.

## A (built): per occasion

| block | cycles a job (II) | latency | DSP | BRAM18 | LUT | FF | source |
|---|---|---|---|---|---|---|---|
| front | 1 + 1 + 48 x 256 = **12,290** | ~5 | 0 | 0 | ? | ? | lane loop: a word a cycle (the window copy); resources **unknown -- probe: `prach_front` at W = 128** |
| fft1024, `SsrFft` L=1024 | 48 x 256 = 12,288 | 1,276 (first frame, measured) | **48** | **50** | **21,400** | **24,713** | `dsp/ssr_fft/index.md` (interval L/R by construction, RTL); `examples/ssr_fft/resource.md` (csynth, ping-pong, xczu48dr 250 MHz, Vitis 2025.1) |
| extract | 12,288 in; then 1 + 8 + 140 out | ~10 + 149 | 0 (adders) | ~4-8 (2 x 4 banks x 35 x 31 b, or LUTRAM) | ? | ? | lane loop, 4 bins a word at II = 1 (DEPENDENCE false: one address revisited only n_ant x 256 cycles later); resources **unknown -- probe: `prach_extract`** |
| correlate | 149 in; 11 x 4 x 64 = **2,816** out | ~10 | ~16 (4 lanes x 4 16x16 products) + ~8 (two constant modulos a lane) | ~4 (ROM 4 x 2 x 278 x 16 b) + 2 (Y banks) | ? | ? | lane loop; DSP by count of products (`resource_model/index.md`: one DSP48E2 a 16x16 product is the rule, the modulos are a guess); **unknown -- probe: `prach_correlate`** |
| fft256, `SsrFft` L=256 | 44 x 64 = 2,816 | 350 (measured) | **36** | **14** | **15,005** | **13,335** | as fft1024 |
| detect | 11 x (4 x 64 + 64 + ~6 x (43 + 5 + ~10) + 64 + ~10) = **~7,000** | ~10 | 8 (4 lanes x 2 18x18 squares) + ~5 (the 38 x 9 noise count and 16 x 46 threshold products) | ~2 (P: 4 x 64 x 38 b; the mask in LUTs) | ? | ? | lane loop for the profile; the mask set (64), the window scan an element a cycle with a fill per window (~6, assumed) and its 5-sample exclusion, the noise pass (64), the decide pass (decision 17); **unknown -- probe: `prach_detect`** |

**Bottleneck: the input, 12,290 cycles a job** (front and fft1024 run at one word a cycle; nothing
else is slower).  A job every 12,290 cycles = **49 us**, against 500 us: **10x headroom** at the worst
case, 4 antennas.  The stages downstream overlap the next job's input (correlate 2,816, detect ~7,000:
both under 12,290), so the interval is the input's.  (Note: detect is slower than correlate per root,
~640 against 256 cycles, so the FFT-256 is back-pressured within a job -- harmless here, the job
still ends inside the input's interval; a ping-pong profile buffer would remove it if a workload
needed it.)

**Latency** from a job's last input word: fft1024 1,276 + extract 149 + correlate's frames 2,816 +
fft256 350 + detect's last root ~640 + headers ~50 = **~5,300 cycles = 21 us** (sum of the rows; the FFT
latencies measured, the rest lane-loop counts).  Requirement 500 us: met.

**pysim** (uncalibrated: the built stages charge one cycle a word at their ports and nothing for
compute; the FFTs are the library's timed models): the "test" scenario's four answered jobs (106
windows) end at 32,180 cycles; the input alone is 27,136 words.  Consistent with an input-bound
pipeline; it is not a timing claim.

**Utilization** of the xczu48dr (4,272 DSP, 425,280 LUT, 2,160 BRAM18, `systems/library.md`, *The part*):

| | DSP | BRAM18 | LUT |
|---|---|---|---|
| the two FFTs (measured) | 84 (2.0 %) | 64 (3.0 %) | 36,405 (8.6 %) |
| the four built stages (by count / guess) | ~35 (0.8 %) | ~15 (0.7 %) | **unknown** (a few thousand each is typical of lane loops this size: a guess, not a number) |

Far under the ~70 % rule of thumb: it fits, and the part could hold several searchers (more antennas,
more FDM occasions) if the input could feed them.

**The probes that settle the unknowns** (each `python prach_build.py --through <stage>_kernel_resources`
on a machine with Vitis 2025.1):

1. `extract_csynth`: II = 1 on `FRAMES` with the rotated-lane accumulator (the bank mapping is the
   risk), and its LUTs.
2. `correlate_csynth`: II = 1 on `CORR` with four ROM reads and two constant modulos a lane; DSPs.
3. `detect_csynth`: II = 1 on `PDP` and `WIN`; the fill per window.
4. `front_csynth`: II = 1 on `WIN`; LUTs.
5. `csynth` (the composite): the first build that nests `SsrFft` in another composite
   (`dsp/ssr_fft/build.md`: "not yet exercised"); timing at 4 ns.

## B: per occasion

| block | cycles a job | DSP | LUT | source |
|---|---|---|---|---|
| NCO + decimating FIR (x4), 4 lanes in | 12,288 | ? | ? | **unknown -- probe**: CORDIC / NCO has no library block; FIR from `firblock` |
| FFT-256 per window (`SsrFft` L=256), 1 sample out a cycle | 48 x 64 = 3,072 | 36 | 15,005 | measured, as above |
| back end as A | 2,816 / 5,600 | 36 + ~35 | 15,005 + ? | as above |

Same interval (input-bound, 12,288), saves ~6.4k LUT and 12 DSP net of a second FFT-256 only if the
NCO + FIR cost less than the FFT-1024's 21,400 LUT -- unknown, and it adds two built blocks with their
own numerics.  Not worth it here.

## C: per occasion

| part | time a job | source |
|---|---|---|
| hardware (front, fft1024, extract) | 12,290 cycles = 49 us | as A |
| Y to memory (`MemWStream`, 149 words) | ~200 cycles | `timing_model/memstream.md` model, not run: **assumed** |
| software back end, 36,700 complex multiplies = 146,800 `cdot_q15` elements | **1.34 ms** on one A53 core (36 calls of n = 4096) | `CpuPlatform.sw_function("cdot_q15").price`, A53 1.2 GHz platform (`cpu/dse.md`); mapping: a complex multiply = 4 elements, adds and the FFT's data movement ignored -- optimistic |

**Does not fit** the worst case: 1.34 ms > 0.5 ms on one core (2.7 cores needed, more with the
ignored overheads).  Fits one occasion per 10 ms frame.  It saves the FFT-256 (36 DSP, 15k LUT) and
two stages -- 3.5 % of the part's LUTs -- at the cost of a memory path and most of the A53.

## The choice

**A.** It meets the worst-case rate with 10x headroom, uses library FFTs for 94 % of the
multiplies, and leaves the processor free.  C is the fallback if the occasion rate is known to be one
per frame and LUTs are precious; B only if a probe showed its NCO + FIR to be much cheaper than an
FFT-1024 (unlikely, and the FFT-1024 is shared with PUSCH in a full receiver).

## Detection performance (measured in simulation, not priced)

The detector of decision 17 (noise less the window peaks, a -16 dB relative rule, a 4-sample
margin).  `scratch/perf.py` (float) and `scratch/perf_fixed.py` (bit-exact), B4, N_CS 23, 64
preambles, one UE uniformly in the usable zone (the N_CS zone less 1 us), a random phase per antenna
(flat), AWGN, SNR per subcarrier in the PRACH band:

| | threshold `thr_q8` (Pfa 1e-3 / occasion, 20,000 noise-only occasions) | Pd at -24 dB | -22 dB | -20 dB | -18 dB |
|---|---|---|---|---|---|
| 4 antennas, float | 1531 (5.98 x mean) | 0.782 | 0.994 | 1.000 | 1.000 |
| 2 antennas, float | 2465 (9.63 x mean) | 0.250 | 0.714 | 0.990 | 1.000 |
| 4 antennas, **bit-exact** (with a PUSCH interferer 10 dB over noise) | 1531 | 0.787 (float, same input: 0.780) | 1.000 (1.000) | | |

Bit-exact against float on the same input: no false alarm in 300 noise-only occasions (either); the
same detections in 99 % (-24 dB) and 100 % (-22 dB) of occasions -- the differences are
near-threshold; peak / mean ratio error mean 0.9 %, max 4.7 % over 268 detections.  500 occasions a point (float) and 150 (bit-exact): the Pd numbers carry +-2 to 3 % of
Monte-Carlo error.  A detection counts only at the right preamble with the delay within 0.52 us;
another preamble was reported in <= 0.4 % of occasions.  (Decision 10's detector, for comparison:
`thr_q8` 1439 / 2207, Pd 0.784 / 0.996 at -24 / -22 dB with 4 antennas -- the same sensitivity.)

**The threshold table** (decision 18, `prach/thresholds.py`, `scratch/thr_table.py`): `thr_q8` for
1, 2, 4 antennas and every N_CS of Table 6.3.3.1-7, 64 preambles, Pfa 1e-3, 20,000 noise-only
occasions a point simulated as white Gaussian Y.  It hardly depends on N_CS -- 4 antennas 1497..1582
(5.8 .. 6.2 x the noise mean), 2 antennas 2455..2709, 1 antenna 4263..5290, the spread mostly
Monte-Carlo (+-2 % to +-5 %) with the smallest N_CS (2, 4) highest -- and the shortcut agrees with the
full float chain at N_CS 23 (1525 / 2496 against 1531 / 2465).  The bit-exact chain against the
shortcut, 300 noise-only occasions at four points (`results/thr_check.json`): the median and 90th percentile of the per-occasion maximum agree within
-2 % .. +5 % with no consistent sign (1 antenna N_CS 0 / 13, 2 antennas N_CS 46, 4 antennas N_CS 23),
and 1 of 1,200 bit-exact occasions crossed its threshold (0.3 % at 4 antennas, 300 occasions; 1.2
expected at Pfa 1e-3) -- the fixed-point chain does not move the false-alarm rate measurably.
The scenarios' old 1-antenna value (4096) was a placeholder 10 % under the calibrated 4546: at N_CS 0 it
gives Pfa 3.0e-3 (0.85e-3 at 4546; 20,000 fresh occasions).

**Robustness** (`scratch/robust.py`, `results/robust.json`; 300 occasions a point, float, bit-exact
spot checks in `bit_exact_checks`; 4 antennas):

| what | result |
|---|---|
| residual CFO at -20 dB | Pd 1.0 to 800 Hz, 0.97 at 1 kHz, 0.30 at 1.5 kHz: the 12 repetitions summed coherently over 0.43 ms null at 1/0.43 ms = 2.3 kHz.  38.101-1 leaves a UE +-0.1 ppm of its received carrier (~350 Hz at 3.5 GHz) plus Doppler (from memory, not checked here): fine for low speed; high speed needs non-coherent group combining |
| multipath (5 Rayleigh taps over 1 us, ~0.3 us rms; an assumed profile, not 38.141's) | Pd 0.67 / 0.90 / 0.98 / 1.0 at -22 / -20 / -18 / -16 dB |
| timing error | flat: mean ~0, rms 38 ns, all within one TA step (0.26 us); multipath: +52 ns bias (the strongest tap, not the first), rms 83 ns, 98.5 % within one TA |
| near-far, same root, in the usable zone (`scratch/near_far_usable.py`) | the weak UE (-16 dB) found at gaps of 0-13 dB (1.0), 15 dB (0.97; bit-exact 0.96 in 100), not at 18 dB and above (the relative rule); another preamble reported <= 0.3 %.  (`robust.json`'s own near-far rows let the delays run to the zone less 0.6 us, past the usable zone: 1-5 % ghosts there, as decision 17 predicts) |
| a strong UE (0 / 20 dB) at zero delay | no other preamble reported (`margin_rel.json`; decision 10's detector: 30 %) |

## Open questions (most important first)

1. The four csynth probes above -- until then the built stages' resources are unknown.
2. Whether TS 38.141-1's B4 conformance SNR points are met: compare its table to the curve above
   (and with its TDLC300-100 channel; the multipath here is an assumed stand-in).
3. Near-far beyond ~16 dB on one root, and CFO beyond ~1 kHz (above): both need a second pass or a
   different combining, not designed here (`spec.md` open questions 5, 6).
