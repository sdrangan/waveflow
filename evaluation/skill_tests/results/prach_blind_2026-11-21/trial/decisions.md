# Decisions taken without you

Each entry is a point where I would have stopped for your decision.  I took the recommendation and
carried on; every one can be revisited.  The plan documents mark the same choices **assumed**.

## 1. Which PRACH: long (839) or short (139) preambles, and which format?

- **Options.** (a) Long format 0, L = 839 at 1.25 kHz: a 24,576-point FFT at 30.72 Msps (not a power
  of 4: no library FFT) or decimation to 1.92 Msps and a 1,536-point FFT (still not a power of 4);
  (b) short formats at 30 kHz (A1..C2), L = 139: a preamble symbol is exactly one 1,024-sample OFDM
  symbol, so the PRACH is 139 bins of a 1024-point FFT -- which the library's `SsrFft` does at full
  rate.
- **Recommended: (b), with B4 (12 repetitions) as the design point**, every short format accepted
  (1 to 12 repetitions are a command field).  Short formats are the FR1 TDD choice at 30 kHz, and B4
  is the most expensive of them, so the sizing is the worst case.

## 2. How many receive antennas?

- **Options.** 1, 2, 4.  **Recommended: 4** -- the RFSoC 4x2 brings out four ADCs; the design takes 1 to
  4 per command and the cost is linear in it.

## 3. Which N_CS / cell size?

- **Options.** zeroCorrelationZoneConfig 0..15 (N_CS 0..69); larger N_CS = larger cell, more roots.
- **Recommended: N_CS = 23** (config 11): 6 shifts a root, 11 roots, ~0.8 km.  A command field; the
  hardware supports N_CS 0..139 (0 = 64 roots, the back end's worst case, still within budget).
  Unrestricted sets only (no high-speed sets).

## 4. What enters the accelerator: continuous samples, or the occasion's FFT windows?

- **Options.** (a) A continuous 30.72 Msps stream per antenna, with the kernel finding the occasion by
  frame timing and dropping the CP; (b) the windows, CP removed, symbol-major then antenna, cut
  upstream by the capture front-end that already knows the frame timing.
- **Recommended: (b).**  Frame timing is a property of the radio's capture, not of the searcher, and
  (b) keeps the searcher a command-response kernel (`docs/guide/systems/command_response.md`).  The
  cost: an upstream buffer of one symbol per antenna (4 x 1024 samples) to interleave antennas.

## 5. The architecture: A, B or C (`plans/arch.md`, priced in `plans/eval.md`)?

- **Options.** A: full-band FFT-1024, bin selection, frequency-domain correlation, FFT-256, peak search
  -- all in hardware.  B: NCO + decimating FIR to 7.68 Msps, then FFT-256s.  C: hardware to Y, the
  back end in software on the A53.
- **Recommended: A.**  Meets one occasion per slot with 10x headroom; 94 % of its multiplies are in
  library FFTs; leaves the A53 free.  C fails the per-slot rate on one core (1.34 ms > 0.5 ms); B adds
  two blocks no library covers to save at most 6k LUT.

## 6. Which Waveflow frame?

- **Recommended: `stream_pipeline`** -- stages streaming to each other, one input, results and status
  out, no memory, no bus.  (`freerun_pipeline` needs a memory adaptor; `bus_system` a host on a bus.)

## 7. The correlation FFT: inverse, or forward on the conjugate?

- **Options.** `SsrFft(inverse=True)` on `Y conj(R)`; or the forward FFT on `conj(Y) R`.
- **Recommended: forward.**  Same magnitude at the same index; the forward path is the one with RTL
  measurements, and one guide page says the inverse "is not built yet" while another documents it.

## 8. The root reference sequence: stored or generated?

- **Options.** A ROM of all 138 roots' 139-point DFTs (19,182 complex entries, ~19 BRAM18); a table the
  host loads per configuration (a second path to keep in order with the commands); generated on chip
  from `(u, u^-1)` -- the DFT of a prime-length ZC root is `c conj(x_u(u^-1 k))` (checked
  numerically, `tests/test_prach.py::test_reference_is_the_root_dft`).
- **Recommended: generated**, from a 278-entry cos/sin ROM and two constant modulos a lane; software
  sends `u^-1` with `u` (rare, cheap) and front checks `u u^-1 = 1 mod 139`.

## 9. Fixed-point formats

- **Recommended** (all bit-exact in `prach/golden.py`): the FFTs unscaled (the library's only mode);
  Y block-floating with one exponent per occasion (no AGC assumption past the ADC, no overflow
  possible); the reference Q15; Z halved and rounded to int16 (bounded at 2^14.5, cannot saturate);
  the profile from the FFT-256 output rounded to 18 bits (bounded at 2^16.2).  Checked against
  float: peak/mean within 0.9 % on average (max 4.4 %), the same Pd within Monte-Carlo error, no false
  alarm in 300 noise-only occasions (`results/perf_fixed.json`).

## 10. The detection statistic and the threshold

- **Options.** Peak over (a) the root's own profile mean (signal included), (b) a noise estimate over
  all roots, (c) a CFAR excluding the peak's neighbourhood.
- **Recommended: (a)** -- local, streaming, no second pass; at high SNR the signal inflates the mean by
  at most 1/256 of a peak, harmless.  The threshold comes from 20,000 noise-only occasions per antenna
  count at Pfa = 1e-3 per occasion: `thr_q8 = 1439` (4 antennas), `2207` (2), sent per command.
- **Superseded by decision 17:** "harmless" was wrong for a second UE on the same root (near-far);
  the noise now excludes the window peaks, with a relative rule, and the thresholds are recalibrated.

## 11. A window margin

- Found by the test scenario: a strong UE at zero delay also lit the neighbouring preamble, because
  its peak sits at a fractional index and half the main lobe falls on the sample below the window.
- **Recommended: start each window 2 samples (260 ns) early** (`golden.WIN_MARGIN`).  The ghost is
  gone (another preamble reported in <= 0.2 % of occasions over the SNR sweep); the zone's last 260 ns
  belong to the next shift, so the usable cell radius shrinks by ~40 m.
- **Superseded by decision 17:** 2 samples hold the main lobe but not the first sidelobe; the margin is
  now 4 (520 ns), and the usable zone ends ~1 us before the N_CS zone's end.

## 12. Errors

- **Recommended:** the command-response contract -- every error fatal, reported by the last stage,
  nothing answered after it.  Because the FFTs count words, an early TLAST in a window is zero-filled
  to the job's frame count before front stops (so no stage is left mid-frame).

## 13. The process's Stage 1 stop

- The `stream_pipeline` process says to stop after Stage 1 (spec, golden, scenarios) for approval;
  your instructions say to carry on with my recommendation wherever I would stop.  **I carried on**
  into Stage 2 (the stages, the composite, codegen), and recorded it here.

## 14. What could not be run here

- Vitis HLS / Vivado are not installed (`csynth`: "Vitis installation not found"), and neither are the
  `ap_int` headers, so the four hand-written HLS bodies (`src/`) are **written but not compiled**.
  Every gate that needs them (csim, csynth, XSI `compare`) is on the DAG and was not run; pysim
  against the golden is the gate that ran.  Resources of the built stages are "unknown -- needs a csynth
  probe" in `plans/eval.md`.

## 15. Part and clock

- **Recommended: xczu48dr (RFSoC 4x2) at 250 MHz** -- where every library model is filed and both FFTs
  are measured.  The frame's default (a Zynq-7020 at 100 MHz) has no measurement of these FFTs, and
  at 100 MHz the input bound would be 123 us a job (still inside 0.5 ms, but with 4x less headroom).

## 16. After the build: what to do next, with no Vitis on the machine?

- **The pause.** Stage 2 is done as far as this machine allows (pysim bit-exact, codegen); you asked
  me to record the pause and carry on.
- **Options.**
  (a) C-simulate the four HLS bodies with g++ against the golden's link messages, using AMD's
  open-source `ap_int` / `ap_fixed` headers -- **tried and blocked**: this session may not download
  them (`git clone` of `Xilinx/HLS_arbitrary_Precision_Types` was refused), and writing my own
  `ap_fixed` emulation would check my code against my reading of the vendor's semantics, which proves
  little.
  (b) Add timed phases to the built stages' pysim -- of little worth with no RTL to calibrate them
  against; the lane-loop counts in `plans/eval.md` say as much.
  (c) **Characterise the detector where `plans/` left it open**: residual frequency offset, multipath
  (a delay spread inside the zone), several UEs at once with a near-far power gap, and the
  timing-estimate error -- with the bit-exact model where it matters.  These are the design's real
  unknowns that need no tools, and they can change the design (as the window margin did).
- **Recommended: (c)**, then fold what it finds into `plans/eval.md` and `results/report.md`, and fix
  the design if it shows a defect.  (It did: decision 17.)

## 17. The noise estimate, the sidelobes and the window margin (a defect found by decision 16)

- **What was found** (`scratch/robust.py`, `results/robust.json`, bit-exact confirmed): with the noise
  taken as the root's whole-profile mean (decision 10), a UE 20 dB under another on the same root is
  **never** detected (Pd 0 at gaps of 20 and 30 dB; 1.0 at 10 dB).  The strong UE's main lobe and
  sidelobes put enough energy into its root's profile to lift the mean, and so the threshold, over the
  weak UE's peak -- decision 10's "harmless" was wrong.  Then
  `scratch/taper.py` and `scratch/margin_rel.py` found a second, older defect: a strong UE **at zero
  delay** lights the previous shift's window in ~30 % of occasions (0 dB and 20 dB alike), because its
  first sidelobe (-13 dB, 1.43 ZC samples early) falls past decision 11's 2-sample margin.  UEs close
  to the gNB are the common case, so both matter.
- **Options weighed** (float, B4, 4 antennas, every variant recalibrated at Pfa 1e-3;
  `results/noise_est.json`, `relrule.json`, `taper.json`, `margin_rel.json`, 200 occasions a point):
  - *the noise estimate*: (A) own-root mean, as built; (B) the mean over every root -- identical to A
    by Parseval (every root's profile carries all of Y's energy), so no help; (C) own root less +-2
    samples round each window's peak -- finds the weak UE at 15 and 20 dB (Pd 1.0) but exposes the
    strong UE's sidelobes: another preamble reported in 9-17 % of occasions; (D) C over every root --
    fails like A (the strong UE's energy is flat on the other roots, nothing to exclude).
  - *against the sidelobes*: a spectral taper on Y (Hann / Hamming: sidelobes -31 / -43 dB) -- costs
    1.5 dB (Pd at -24 dB 0.76 -> 0.42 / 0.53) and widens the main lobe into the next window (ghosts
    13-19 % with multipath): rejected; a relative rule (report a peak only above gamma x the root's
    strongest window peak) at gamma = -13 .. -25 dB; a window margin of 2 or 4 samples.
  - the result that decides it (`margin_rel.json`, "found" = the weak UE / ghost = another preamble):

    | | near-far 15 dB | 20 dB | strong 20 dB, uniform delay | at zero delay | at zone - 0.8 us | TDL 10 dB | Pd -22 dB |
    |---|---|---|---|---|---|---|---|
    | A, margin 2 (as built) | 0 / 0 | 0 / 0 | 0.5 % | **34 %** | 0 | 0 | 0.99 |
    | C, margin 4 | 1.0 / 4 % | 1.0 / 8.5 % | 20 % | 99 % | 99 % | 11 % | 0.99 |
    | **C + gamma -16 dB, margin 4** | **0.955 / 0** | 0 / 0 | **0** | **0** (1.5 % at 0 dB) | 68 % | 5.5 % | **0.99** |
    | C + gamma -19 dB, margin 4 | 1.0 / 0 | 0.095 / 0 | 0.5 % | 53 % | 99 % | 8.5 % | 0.99 |

  A UE late in its zone sidelobes into the next shift's window whatever the margin (the margin only
  moves the boundary): the usable round-trip delay, delay spread included, ends ~1 us before the
  zone's end (margin 0.52 us + the first sidelobe 0.35 us + its width).  The TDL ghosts above are of
  that kind -- those runs let the taps reach 0.2 us from the zone's end.
- **Recommended: C + gamma = -16 dB + WIN_MARGIN = 4**, and the cell planned on the usable zone: the
  N_CS zone less 1 us (N_CS 23: 4.5 us round trip, ~0.68 km instead of ~0.8 km; a larger cell takes
  N_CS 27, a command field, nothing in hardware changes).  Near-far rises from 10 to ~15 dB, the
  zero-delay ghost goes from 34 % to 0, sensitivity is unchanged.  Gaps beyond ~16 dB on one root stay
  undetectable with a profile-domain detector: that needs interference cancellation (subtract the
  strong UE and search again), a second pass this design does not have -- noted as an open question.
- **What changes:** `golden.detect_root` / `search_float` (`NOISE_EXCL = 2`, `REL_Q = 40`: the rule is
  `peak N 2^8 > thr_q8 S` with `S, N` the masked sum and its count, and `40 peak > max`, a shift-add);
  `WIN_MARGIN = 4`; `DetRec` gains `n_noise` (U16, in the word's 16 spare bits); the detect HLS body
  gains a mask, a 64-cycle noise pass and a decide pass a root (~+130 cycles a root, ~5,600 -> ~7,000
  a job: detect stays under the input's 12,290); `THR_Q8` recalibrated (`results/perf_float.json`:
  1531 for 4 antennas, 2465 for 2; Pd unchanged, 0.782 / 0.994 at -24 / -22 dB); two new tests
  (near-far, no ghost).  **Done**, 22 tests pass.  Confirmed in the usable zone
  (`results/near_far_usable.json`): the weak UE found at gaps to 13 dB (1.0) and 15 dB (0.97, bit-exact
  0.96), another preamble reported in <= 0.3 % of occasions.

## 18. After decision 17: what next?

- **The pause.** The detector is fixed and characterised at the design point; you asked me to record
  the pause and carry on.
- **Options.**
  (a) A second search pass with interference cancellation, for near-far beyond ~16 dB -- a new
  stage (or a loop in detect) and a model of the strong UE's response; large, and the need is not
  established (power ramping usually keeps arrivals within 15 dB).
  (b) Non-coherent combining of repetition groups in extract, for CFO beyond ~1 kHz -- a real but
  narrow need (high speed); it costs sensitivity at low CFO, so it would be a command option.
  (c) **The threshold the host sends.** `thr_q8` is calibrated only at N_CS 23 with 64 preambles, for
  4 and 2 antennas; the 1-antenna value (4096) is a placeholder, and the scenarios already send N_CS 0
  and 46.  The false-alarm rate depends on how many windows are searched and how long they are (N_CS,
  n_pre) and on the antennas -- not on the format: noise after the coherent repetition sum is the
  same white Gaussian Y whatever `n_sym`.  So software needs a table `thr_q8(n_ant, N_CS)`, and
  without it every configuration but one runs at an unknown Pfa.
- **Recommended: (c)**, cheap and a correctness gap: calibrate `thr_q8` for n_ant 1, 2, 4 and every
  N_CS of Table 6.3.3.1-7 (64 preambles) at Pfa 1e-3 per occasion, simulating the noise directly as Y
  (white Gaussian on the 139 subcarriers: skips the FFT-1024, which noise passes through unchanged in
  distribution); check the shortcut against the full-chain values at N_CS 23 (1531 / 2465); put the
  table in `prach/thresholds.py` for the host and the scenarios; bit-exact spot check of Pfa at a few
  table points.  n_pre < 64 searches fewer windows, so the 64-preamble value is conservative there.
  (a) and (b) stay open questions.
- **Done.** `prach/thresholds.py` (`thr_q8(n_ant, n_cs)`; 3 antennas falls back to the 2-antenna
  value, conservative), used by the scenarios; `test_threshold_table`; 23 tests pass, the DAG's pysim
  passes.  The threshold hardly depends on N_CS (4 antennas 1497..1582, 2: 2455..2709, 1: 4263..5290);
  the shortcut matches the full chain at N_CS 23 (1525 / 2496 against 1531 / 2465) and the bit-exact
  chain at four points (`results/thr_check.json`).  The old 1-antenna placeholder (4096) ran at Pfa
  3e-3.  The scenarios' detections are unchanged.

## 19. After decision 18: what next?

- **The pause.** The detector and its thresholds are done; you asked me to record the pause and carry on.
- **Options.**
  (a) Interference cancellation for near-far beyond ~16 dB (decision 18 (a)) -- large; need unproven.
  (b) Non-coherent combining of repetition groups for CFO beyond ~1 kHz (decision 18 (b)).  A
  residual of ~1 kHz is ~0.35 kHz of UE error plus twice the Doppler -- about 100 km/h at 3.5 GHz;
  a real limit for a highway cell, not for the small TDD cell this design is sized for.
  (c) **An independent review of the four HLS bodies against the golden.**  They are the least
  verified part of the deliverable: no compiler has seen them (decision 14), pysim checks the
  Python stages, not the C++, and the detect body was just rewritten (decision 17).  A reviewer that
  has not read my reasoning, given the golden, the stages and the bodies, looking for arithmetic that
  differs from the golden (rounding, widths, signedness, wrap), framing and stream-count mistakes that
  would deadlock, and HLS constructs that will not synthesise at II = 1.
- **Recommended: (c)** -- it is the most likely place for a defect that the user would hit first (at
  csim, on a machine with Vitis), and it needs no tools; then fix what it finds that I can confirm,
  and record what it finds that I cannot settle without a compiler.  (a) and (b) stay open questions;
  (b) is noted in `plans/spec.md` with the speed it corresponds to.
- **Done.** Two reviewers (front + extract; correlate + detect), each reading the bodies against
  `prach.py`, `golden.py`, the generated headers and tops, found **no arithmetic, protocol or
  compile defect** (scaling, rounding, block exponent, ROM index, windows, mask, both tests, word
  counts, TLAST, error paths all checked).  They found four places where a loop would likely miss the
  II = 1 its comment claims -- throughput, not correctness -- all fixed:
  1. correlate's lane ROMs were one `[4][278]` array a table (4 reads a cycle from a 2-port ROM:
     II 2, correlate at half rate).  The generated `prach_rom.h` now has one array per lane and an
     inline `cos_at(lane, p)` / `sin_at(lane, p)` (`prach_build.py::render_rom`);
  2. `roots[128]` in front, extract and correlate, written / read 16 bytes a cycle, unpartitioned
     (II ~8 on <= 8 words a job): `ARRAY_PARTITION cyclic factor=16`;
  3. extract's `amax`: eight compare-selects in series on the loop-carried maximum (II 2-3 likely at
     4 ns, on the 12,288-word input loop -- the pipeline's bottleneck): the word's eight magnitudes
     now reduce as a tree, then one compare;
  4. detect's noise sum: four conditional 46-bit adds in series on the loop-carried total: the row's
     lanes now sum first, then one add.
  Noted, not changed: front accepts a roots / window burst *longer* than announced by reading on
  (pysim drops the excess) -- the contract defines no error for it; at small N_CS detect's window
  loop costs ~15-20 cycles a window (<= 64 windows a job: <= ~1,300 cycles), above the plan's
  estimate but under the input bound.  csynth remains the judge of all four fixes.  Codegen passes,
  23 tests pass.

## 20. After decision 19: what next?

- **The pause.** The review is folded in; you asked me to record the pause and carry on.
- **Options.**
  (a) / (b) interference cancellation, CFO group combining -- unchanged since decision 18.
  (c) **A missing TLAST (a burst longer than announced)** -- the case decision 19's review noted and
  I left.  Today front reads straight on into the next burst's words: every later job is computed on
  misaligned data and answered `OK` with garbage detections, and pysim (which drops the excess) does
  not even model it.  A silent desynchronisation is the worst failure a command-response kernel can
  have; the contract (`docs/guide/systems/command_response.md`, decision 12) is that errors are
  fatal and reported.  The fix is small: a new status `TLAST_LATE`, front checking TLAST on the last
  word of the roots and of every window, then the same path as `TLAST_EARLY` (zero-fill the job's
  remaining FFT frames, report, halt); the Python model reading whole bursts so it sees the excess;
  a scenario `late` and its tests.
  (d) A host-side driver module (build a command, read the answer, timing advance) -- convenience;
  `scenario.make_cmd` and `golden.delay_estimate_s` already do the work.
- **Recommended: (c)** -- a correctness gap in the interface contract, cheap, and testable end to end
  in pysim.  The `PrachCmd` itself is one word, so a longer command burst is the same case one level
  up; front checks TLAST there too.
- **Done.** `StatusCode.TLAST_LATE = 4` (the enum's generated header regenerated); front's Python
  model reads whole bursts (`get()`) and compares their word counts -- the command 1, the roots
  `ceil(2 n_roots / 16)`, each window 256 -- and its HLS body checks TLAST on those last words (the
  command through the schema's own framed / AXI-Stream reader, which returns the TLAST status
  `read_msg` does not); after a late window that window goes to the FFT whole and the job's remaining
  frames are zeros, as for TLAST_EARLY; nothing past the excess is read.  Three scenarios, "late"
  (window 1), "lateroots", "latecmd", each run through every stage alone and the composite and on
  the build DAG; `test_errors_are_fatal` covers them.  38 tests pass.  Also made the early-roots rule
  word-level in the scenario model (as front and the hardware count words; no scenario used the
  old byte-level rule).
