# Blind run: "Build a 5G NR PRACH searcher for a reasonable config in a 20 MHz channel" (2026-11-21)

**Skill tested:** `waveflow-architecture` as first written (PR #332, commit `e882291`: four documents,
spec / arch / eval / per-block specs, library blocks first, pauses after spec and eval), with
`docs/guide/systems/library.md`. PR #332 has since replaced that skill with a short chat answer
(`5ef15a7`..`d1e94d2`). This run measures the old version.

## How the run was made

- `waveflow blind-test --init --model opus` was run in a fresh folder outside the clone. `waveflow init` installed the eight
  `waveflow-*` skills. The agent had the Waveflow MCP server and nothing else. Project memory was turned off
  (`CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`).
- The spec was `trial/prach.md`: the one-line prompt, plus "I'm away. At each decision point, write the question,
  the options and your recommendation in `decisions.md`, then proceed". It also said there was no Vitis. Each
  time the agent stopped, it got this reply: *"I'm still away. Record this pause in decisions.md with your
  recommended choice, then proceed with it."* (4 rounds).
- **Leak check** (`run_summary.md`, *What it read*): the agent read nothing in `plans/`, `evaluation/` or
  `answers/`. Its reads outside its folder were Waveflow library source (`waveflow/dsp/ssr_fft/*`,
  `hw/interface.py`, `build/composite_gen.py`), which a user of an installed package also has.
- Model Opus 5.5. The agent made 828 tool calls over 5 phases in **70 min wall time** (phase 1 alone took 28 min). Tokens: 149M in
  (148.5M of them cached), 0.30M out.

## What it came up with

| | |
|---|---|
| **Config** | FR1 TDD, 30 kHz SCS, 51 PRBs, 30.72 Msps. Short preamble, L=139. Design point **format B4** (12 repetitions), and every short format is accepted. **4 Rx antennas**. **N_CS = 23** (zeroCorrelationZoneConfig 11): 6 shifts a root, 11 roots for 64 preambles, a cell of about 0.68 km usable. One occasion per 0.5 ms slot. Unrestricted set. |
| **Key insight** | A short-preamble symbol is exactly one 1024-sample OFDM symbol at 30.72 Msps. So the PRACH is 139 bins of a 1024-point FFT, and no resampling is needed. |
| **Architecture (A, built)** | front (command check, windows) → `SsrFft` L=1024 → extract (139 bins, sum the repetitions, block exponent) → correlate (conj(Y)·ZC, root generated on chip from a cos/sin ROM) → `SsrFft` L=256 → detect (power over antennas, window peaks, threshold, answer). A command-response stream pipeline. |
| **Rejected** | B: NCO + decimating FIR, then FFT-256 (its cost is unknown, and it adds blocks with no library match). C: the back half in software on the A53 (1.34 ms per occasion > 0.5 ms). |
| **Bottleneck** | The input: 48 windows × 256 words = **12,290 cycles = 49 µs** at 250 MHz, against a 500 µs budget, for **10x headroom**. Latency is about 21 µs after the last sample. |
| **Cost** | The two FFTs are measured from the library: 84 DSP, about 36k LUT, 64 BRAM18 (2 / 9 / 3 % of an xczu48dr). The four built stages are about 35 DSP by counting multipliers. Their **LUT and FF counts are unknown**, and 5 csynth probes are named. The CPU baseline is 6.7 ms per occasion on one A53 core. |
| **Performance (pysim / Monte Carlo)** | Thresholds are set for Pfa 1e-3 per occasion, with a table over 1/2/4 antennas × all 16 N_CS. With 4 antennas, Pd is 0.78 at −24 dB and 0.99 at −22 dB SNR per subcarrier. The bit-exact model matches float within Monte-Carlo error. Detection tolerates about 1 kHz of frequency offset. Multipath costs about 4 dB (a made-up 5-tap channel). |
| **Code** | The golden, schemas, pysim stages, 4 HLS bodies (never compiled), the build DAG and **38 tests, all passing** (pysim only). |
| **Unknowns it listed** | The LUT/FF counts of the 4 stages, and II=1 for each (csynth). The first nesting of `SsrFft` inside another composite ("not yet exercised" in the FFT docs). The logical-to-physical root table (38.211 Table 6.3.3.1-4), inferred from its pattern and not checked. The 38.141 conformance points. Fading. A UE >16 dB below another on the same root. Frequency offset >1 kHz. The upstream front end (CP removal and windowing) is **assumed**. |

The 20 decisions are in `trial/decisions.md`. Decisions 1-15 are design choices. Decisions 16-20 are "what next?" points it
invented each time it was told to proceed. Each of those led it to find and fix a real defect:

- **Near-far.** A weak UE beside a strong one on the same root was missed, because the strong UE raised the noise estimate. It also saw a ghost
  preamble from the first sidelobe in about 30 % of occasions. The fix: a noise estimate that excludes the peaks, a −16 dB relative rule, and a 4-sample
  margin.
- **Thresholds.** The 1-antenna value was a placeholder, and it gave three times the target false-alarm rate.
- **HLS review.** It reviewed the HLS with two sub-reviewers and found four loops that would miss II=1. None was a correctness fault.
- **Burst errors.** A burst longer than announced corrupted every later job without any error. It became a fatal `TLAST_LATE`
  error, with three new scenarios.

## Domain check (by hand, against TS 38.211 / 38.101-1)

These match the standard:

- 51 PRBs at 20 MHz with 30 kHz SCS.
- L=139 with 12 PRBs at 30 kHz.
- B4 has 12 repetitions.
- Config 11 gives N_CS 23, and the 16-entry N_CS table is right.
- floor(139/23) = 6 shifts, so 11 roots.
- The zone is 5.5 µs, about 0.8 km.

The SNR numbers are plausible. 139 × 12 is about 32 dB of processing gain, so −22 dB per subcarrier is about 10 dB per antenna before
combining. The choice of B4 as the design point for a 20 MHz FR1 carrier is defensible but not the most
common choice. Format 0 (839, 1.25 kHz) or a short format with fewer repetitions are typical in FR1. The agent stated
its reasoning (decision 1) and accepts every short format.

## Where the skill and catalog helped

- **`library.md` sent it straight to `SsrFft`.** It priced the FFTs from measured numbers (resources at L=1024 and
  L=256, Vitis 2025.1). It also used the part's capacity table for utilization, and `CpuPlatform` to price alternative C. Every
  number in `eval.md` names its source, and each unknown names its probe. The format the skill asks for worked.
- **"Alternatives that differ in a real decision"** produced a real B and C, each priced the same way, and the comparison is honest.
- **The command-response and token-per-job rules** carried through to the error handling, and later to the
  `TLAST_LATE` fix.
- The uncovered-blocks list ("sorting / top-k") let it say "no library block does peak search" with a citation.

## Where it misled or cost time

1. **Time.** It spent 28 min before the first stop and 70 min in all, for a question whose architecture answer was
   clear within the first few minutes. The four documents plus the build pushed it into a full implementation. This is
   the finding that the rewritten skill (a chat answer first) addresses.
2. **"Library first" was followed well here** (two FFTs), but the rest was four built blocks anyway. The catalog's
   value was the FFT numbers, not reuse in general. This also supports the rewrite.
3. **The pause semantics.** After phase 1, each "proceed" made it invent a new "what next?" decision (16-20). The
   skill does not say when the work is *done*. Each step was useful, but nothing bounded them.
4. **The "stop after Stage 1" step in the frames process** (`stream_pipeline`) conflicted with the skill's own pause
   points. It recorded that as decision 13.

## Gaps worth fixing (in Waveflow, not the skill)

- **pysim drops the extra words of a burst longer than announced**, where the HLS reads on into the next burst.
  pysim cannot show the resulting desync (decision 19/20). This is related to the known
  `_admit_blocking` depth gap.
- **`SsrFft` nested inside another composite** has never been built (`dsp/ssr_fft/build.md`). This design
  would be the first to do it.
- **`read_msg` does not return TLAST**, so the agent used the schema's raw reader to check it.
- **No library block for a ZC/NCO generator or a peak search**. Both came up in PRACH, and both are common in DSP front ends.
- **pysim cycle counts for built stages are uncalibrated** (one cycle per word at the ports), so the agent could
  not use pysim for timing. It said so.

## Files

- `trial/`: what the agent wrote. Start with `README.md`, `decisions.md`, `plans/{spec,arch,eval}.md` and `results/report.md`.
  The generated headers, `gen/` and `xsi_work/` are left out (they regenerate from `prach_build.py`).
- `run_summary.md`: the harness summary (tool calls, reads, denials, the final message of each phase).
