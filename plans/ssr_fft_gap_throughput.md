# Plan: SsrFft's throughput after a gap -- a future optimization

**Status: PROPOSED 2026-11-22 (the user: post it as a plan for later, not now).** Found by
`plans/osfb_pysim_fidelity.md` (cause A, Sections 5-6 of its record); the pysim *model* of today's behaviour
is that plan's work. This plan is the *hardware* (or design-pattern) fix. Nothing here is started.

## The finding (lab machine, Vitis / xsim 2025.1, `SsrFft` alone, every frame bit-exact)

At `L >= 64` the RTL's interval between frames is `max(2L/R, L/R + g)` for a gap of `g` idle cycles before
a frame: back to back (`g = 0`) it is `L/R`, as designed; **any gap from 1 to `L/R` costs a whole frame
time**. Exact and deterministic (constant and random gaps, `L = 64, 256, 1024`). Set by the last transposer
commutator (`tp{S-2}`, block size `L/16`): its input frame stretches to `2L/R - L/16` cycles, and every
stream after it carries a frame every `2L/R`. At `L = 16` there is no throughput loss (an arrival-phase
latency jitter of 40-43 cycles instead). `L = 4096` not yet measured.

*Update (lab machine, 2026-11-22, `plans/osfb_pysim_fidelity.md` Section 6):* `L = 4096` is measured since -- the
same rule, exact on every constant gap and 23 of 23 random intervals, set by `tp4`. And at `L = 16` constant
gaps of 1-3 cycles do cost throughput, a little: the RTL alternates intervals of 8 and 4 at a 1-cycle gap
(68 cycles over 11 frames against 55 for `L/R + g`); on random gaps its total is `L/R + g`'s.

**What it costs at `L = 1024`, `R = 4`** (a frame is 256 cycles, 1.02 µs at 250 MHz):

| gap before a frame | RTL interval | ideal `L/R + g` | throughput lost |
|---|---|---|---|
| 0 | 256 | 256 | none |
| 1 | 512 | 257 | ~50% |
| 16 | 512 | 272 | 47% |
| 128 | 512 | 384 | 25% |
| >= 256 | 256 + g | 256 + g | none |

Random gaps (a third 0, the rest uniform 1..512): RTL 9,587 cycles against the ideal 8,299, +15.5%. In a
shared-FFT loop whose frames arrive a cycle or so apart -- `examples/os_filterbank`, where `pre` leaves at
least a cycle -- the FFT runs at **half its rated throughput**.

## Why it matters

A shared FFT is the pattern Waveflow teaches (`docs/guide/systems/shared_kernels.md`, `os_filterbank`), and a
shared FFT seldom gets its frames back to back. The FFT's documented interval, "`L/R`, by construction"
(`docs/guide/dsp/ssr_fft/timing.md`), holds only back to back. The FFT-sharing evaluation problems in
`plans/dse_matrix_sched.md` Section 11 (spectrum sensing, delay-Doppler) depend on it.

## Two fixes, to compare

1. **The design pattern (no FFT change).** The loss needs a gap; a producer that holds the next frame whole
   and starts it the cycle after the previous one ends gives `g = 0`. Check whether `os_filterbank`'s `pre`
   (and the arbiter in front of it) can feed the FFT back to back when a frame is waiting, and what that
   costs (a frame of buffering, a cycle of decision). If it works, it is a rule for the shared-kernel guide:
   "feed a streaming unit back to back".
2. **The FFT's HLS.** Make `tp{S-2}` (and any other commutator with the same behaviour) accept a frame that
   follows a gap without losing a frame time -- the interval `L/R + g`. Read the commutator's body and its
   csynth schedule first: why a gap makes it wait for a full window. The usual cause is a loop that restarts
   per frame and only re-enters at its window boundary; the fix keeps the window running across the gap.
   Then: the gap sweep at every `L` (16 to 4096), the back-to-back interval and latency pins unchanged, every
   frame bit-exact, resources and timing at 250 MHz on `xczu48dr` (the RFSoC probe), the pysim model and
   `timing.md` updated to the fixed hardware.

Do 1 first (cheaper, and it may be enough); do 2 if a design cannot keep its producer back to back.

## Done when

- The chosen fix measured at RTL: `os_filterbank`'s FFT busy fraction and end cycles before and after (today
  39-48% busy, ends at 1,067 / 2,424); the gap sweep at every `L`.
- The guide (`ssr_fft/timing.md`, `shared_kernels.md`) and the model match the hardware; gates pass.
