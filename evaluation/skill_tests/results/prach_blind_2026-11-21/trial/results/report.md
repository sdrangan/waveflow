# PRACH searcher -- results

Every gate below names the step that produced its number.  Nothing here was synthesized or run at
RTL: **Vitis HLS and Vivado are not installed on this machine** (`python prach_build.py --through
front_csynth` -> `RuntimeError: Vitis installation not found. Please set WAVEFLOW_VITIS_PATH`), and the
`ap_int` / `hls_stream` headers are not present either, so the hand-written HLS bodies in `src/` have
not even been compiled.

## Gates that ran

| gate | result | produced by |
|---|---|---|
| the 8 message schemas | valid (one benign warning each: "multiple schema classes were found" in the file) | `waveflow_validate_schema` on `prach/schemas.py`, reports in `results/schema_check/` |
| golden: worked example (3 UEs at -5, 0, -8 dB; PUSCH interferer) | the 3 preambles and nothing else; delays within 0.26 us; fixed vs float peak/mean < 1 % | `pytest tests -k golden` |
| golden: the ZC reference generated on chip = the root's DFT up to a constant phase | 4 roots, phase spread < 1e-3 rad | `test_reference_is_the_root_dft` |
| golden: the windows of a root never overlap, every N_CS of Table 6.3.3.1-7 | pass | `test_window_table` |
| errors fatal: last status is the error, all before OK, the next command unanswered | BAD_PARAM, TLAST_EARLY, BAD_ROOT, TLAST_LATE (window, roots, command) | `test_errors_are_fatal` |
| front alone, pysim vs golden links (`m_cmd`, `m_fft`), 6 scenarios | burst for burst, word for word | `test_front_alone` |
| extract alone (`m_y`) | same | `test_extract_alone` |
| correlate alone (`m_cmd`, `m_fft`) | same | `test_correlate_alone` |
| detect alone (`m_out`, `m_status`) | same | `test_detect_alone` |
| the composite (34 tasks, both library FFTs inside), `m_out` and `m_status` | same; "test": 8 + 5 bursts, "early", "badroot", "late", "lateroots", "latecmd": 3 + 2 each | `test_composite`; also the DAG's `pysim`, `<kind>_pysim`, `<stage>_pysim` steps |
| codegen: headers, both FFTs' sources, the ROM, five tops, csynth scripts | generated; the composite top has the 34 tasks and every link at its stated depth | `test_codegen`; `python prach_build.py --through codegen` |

| near-far: a UE 12 dB under another on its root found, nothing else reported (bit-exact, 5 draws) | pass | `test_near_far` |
| a 20 dB UE at zero delay and at the usable zone's end lights no other preamble (bit-exact) | pass | `test_strong_ue_no_ghost` |

| the host's threshold table covers 1-4 antennas x every N_CS, fits U16, agrees with the full-chain calibration | pass | `test_threshold_table` |

38 tests, all passing (`results/pytest.log`, 12 s): each stage alone and the composite now run
on six scenarios ("test", "early", "badroot", "late", "lateroots", "latecmd").

**Thresholds (decision 18).**  `thr_q8` comes from `prach/thresholds.py` by `(n_ant, n_cs)`: Pfa 1e-3
per occasion calibrated for 1, 2, 4 antennas at every N_CS (20,000 occasions a point), checked
against the full float chain and, at four points, the bit-exact chain (`plans/eval.md`).

## Gates on the DAG that did not run (need Vitis 2025.1 + Vivado)

`csynth` and `<stage>_csynth` (and so `kernel_resources`), `system_rtl`, `system_xsi`, `compare` for
the composite on each scenario and for each stage alone.  Run them with
`python prach_build.py` (the composite, through `compare`) and `--through <stage>_compare`.
Expect to debug the four bodies at csim first: they follow the Python model line for line but no
compiler has seen them.  An independent read-through against the golden, the Python stages and the
generated headers (decision 19) found no arithmetic, protocol or compile defect, and four loops that
would likely have missed II = 1 (now fixed: per-lane ROMs, partitioned root buffers, tree-reduced
maximum and noise sums) -- a review, not a compiler.  The composite is also the first build that nests `SsrFft` inside another
composite (`docs/guide/dsp/ssr_fft/build.md` calls this "not yet exercised").

## Detection performance (simulation)

B4, N_CS 23, 64 preambles, one UE in the usable zone, AWGN with a random phase per antenna;
`scratch/perf.py` (`results/perf_float.json`) and `scratch/perf_fixed.py` (`results/perf_fixed.json`):

| | thr_q8 (Pfa 1e-3) | Pd -24 dB | -22 dB | -20 dB | -18 dB |
|---|---|---|---|---|---|
| 4 antennas, float | 1531 | 0.782 | 0.994 | 1.000 | 1.000 |
| 2 antennas, float | 2465 | 0.250 | 0.714 | 0.990 | 1.000 |
| 4 antennas, bit-exact | 1531 | 0.787 | 1.000 | | |

**Robustness and a design change (decisions 16, 17).**  Characterising the detector found two
defects in the first version: a UE 20 dB under another on its root was never detected (the strong
UE raised the noise mean), and a strong UE at zero delay lit the neighbouring preamble in ~30 % of
occasions (its first sidelobe fell past the 2-sample window margin).  The detector now takes the
noise less +-2 samples round every window peak, reports a peak only above -16 dB of its root's
strongest, and starts each window 4 samples early.  Sensitivity is unchanged; near-far on one root
is ~15 dB (Pd 0.97; 10 dB before); no sidelobe ghosts inside the usable zone; the cost is the cell:
round-trip delay plus spread must end ~1 us before the N_CS zone's end (~0.68 km at N_CS 23).
Residual CFO is fine to ~1 kHz (Pd 0.97 at -20 dB), multipath (an assumed 5-tap profile) costs ~4 dB at
Pd 0.98 (-18 against -22 dB), timing is 38 ns rms flat / 83 ns with multipath.  Details and sources: `plans/eval.md`,
*Robustness*; the variants weighed: `decisions.md` 17.

## Sizing (priced, not measured -- `plans/eval.md`)

- Interval: **12,290 cycles a B4 4-antenna occasion (49 us at 250 MHz)**, input-bound; requirement
  0.5 ms.  Latency ~5,300 cycles (21 us) after the last sample.
- The two FFTs: 84 DSP, 64 BRAM18, 36.4k LUT (measured, Vitis 2025.1).  The four built stages: ~35 DSP
  by count; **their LUT / FF / BRAM need a csynth probe** (listed in `plans/eval.md`).
- CPU baseline: 6.7 ms an occasion on one A53 core (priced, `cdot_q15`).

## What the machinery could not express, or where this departs from the references

- The FFT's ports are unframed and frame by count, so a job's identity cannot ride on its data
  through an FFT: it travels on a framed side link (front -> extract, correlate -> detect), and an
  early TLAST is zero-filled to the frame count.  Expressed with the existing pieces; no workaround
  of the framework was needed.
- The built stages' pysim is untimed beyond one cycle a word at their ports (no timed phases); the
  cycle numbers above are lane-loop counts, not pysim measurements.
- The stages' `run_iter` call the golden's arithmetic functions (as `block_eq` does), so pysim against
  the golden checks the streams, the order and the framing -- not the arithmetic a second time.  The
  independent check of the arithmetic is the HLS body against pysim at RTL (`compare`), not run here.
