# PRACH searcher -- architecture

Three alternatives that differ in a real decision; `eval.md` prices each the same way.  **A is
recommended and built** (decision 5 in `../decisions.md`).

## A. Full-band FFT, frequency-domain correlation (built)

**HW / SW split.**  Hardware: everything per occasion -- the 48 FFTs, the bin selection, the 44
correlations and FFTs, the profiles and the peak search (`spec.md` section 4: 1.5 G MAC/s, 140x one A53
core).  Software (`SwFunction`s on the A53, `cpu/index.md`): the configuration (logical -> physical
roots, `u^-1 mod 139`, `k_start`, the threshold) once per configuration change; the timing advance
and the RAR per detection.  Rare and cheap: software.

```
s_in -> front ==win==> fft1024 ==bins==> extract --y--> correlate ==zc==> fft256 ==pdp==> detect -> m_out, m_status
          \_________ cmd: FwdHdr | roots | FwdTrl ____/        \______ cor: CorHdr _________/
```

**The blocks** -- each a kernel (`systems/kernels.md`), one job each; the composite `PrachSearcher` is a
stream pipeline (frame `stream_pipeline`): one input, results and status out, no memory, no bus.

| block | kind | its one job | library? |
|---|---|---|---|
| front | leaf kernel (built) | check the command and roots; copy windows to the FFT; forward the command | built: no library block parses a command |
| fft1024 | library kernel | `SsrFft(L=1024, reorder="pingpong")`, forward | **`SsrFft`**, `dsp/ssr_fft/index.md` |
| extract | leaf kernel (built) | 139 bins of each window, summed over repetitions, block exponent | built: no library block selects / accumulates bins |
| correlate | leaf kernel (built) | per root and antenna, `conj(Y) R_u` with `R_u` generated on chip | built: `VecUnit` could multiply, but not generate the ZC reference at 4 a cycle (see C) |
| fft256 | library kernel | `SsrFft(L=256, reorder="pingpong")`, forward (the correlation) | **`SsrFft`** |
| detect | leaf kernel (built) | profile, window peaks, threshold, the answer | built: no library block does peak search ("sorting, a top-k" is listed as uncovered, `systems/library.md`) |

Why split there: the FFTs are the library's and run by count (256 / 64 words a frame); extract needs a
whole job before it can scale (the block exponent), so it is a stage of its own; correlate runs at a
different rate (44 frames out per 48 in) and holds Y while the next job's windows stream in; detect
needs a whole profile before its window scan.

**Interfaces** (command-response by default, `systems/command_response.md`; every message a schema in
`prach/schemas.py`):

| link | kind | carries per job | width | rate it must sustain | depth |
|---|---|---|---|---|---|
| `s_in` | framed stream, boundary | `PrachCmd`, roots, 48 windows of 256 words | 128 | 1 word / cycle (4 samples; the ADCs need 0.49 words / cycle for 4 antennas at 30.72 Msps) | -- |
| win | stream (unframed: frames by count) | 48 x 256 words | 128 | 1 / cycle | 4 |
| bins | stream (unframed) | 48 x 256 words, `ap_fixed<27,13>` x 4 | 216 | 1 / cycle | 4 |
| cmd | framed stream | `FwdHdr`, roots, `FwdTrl` | 128 | 3-6 words a job | 16 |
| y | framed stream | `ExtHdr`, roots, Y (n_ant x 35 words) | 128 | 149 words a job | 32 |
| zc | stream (unframed) | n_roots x n_ant x 64 words | 128 | 1 / cycle | 4 |
| cor | framed stream | `CorHdr` | 128 | 1 word a job | 4 |
| pdp | stream (unframed) | n_roots x n_ant x 64 words, `ap_fixed<25,11>` x 4 | 200 | <= 1 / cycle | 4 |
| `m_out`, `m_status` | framed streams, boundary | `Resp`, records; `Status` | 128 | a few words a job | -- |

**The token per job** reaches every stage on every path: front always writes `FwdHdr` + `FwdTrl` (with
`nframes = 0` after a header error, so extract reads no frames); extract always writes `ExtHdr`;
correlate always writes `CorHdr` before any frame; detect always answers.  Errors travel in those
headers to detect, which reports them.

## B. Down-convert and decimate first, then FFT-256

Shift the PRACH to DC (an NCO), low-pass and decimate by 4 (7.68 Msps), FFT-256 per window, then
correlate / FFT-256 / detect as in A.

- Saves the FFT-1024: 48 DSP and ~21k LUT, against an NCO and a decimating FIR of ~4 x 8 taps
  per lane -- CORDIC / NCO is in the library's "no block covers" list, the FIR is a worked example
  only (`firblock`), so both are built and their cost is unknown until a csynth probe.
- The FFT-1024 would still be needed for PUSCH in a full receiver, where A could share it.
- More numerics to own (filter ripple across the 139 bins, edge bins).

## C. Hardware front end, software back end

Hardware: front, fft1024, extract (A's first half).  Software: correlation, FFT-256, profiles and
detection on the A53, Y (556 complex) handed over through memory each occasion.

- The device grows a memory writer (`MemWStream`) and the host a buffer: a `bus_system` / device
  design instead of a stream pipeline.
- The back end is 36,700 complex multiplies an occasion -> 1.34 ms on one A53 core (`eval.md`): fits
  one occasion per 10 ms frame, fails one per slot (0.5 ms) unless 3 cores are given to it.
- Flexible (any detector in software), and it saves the FFT-256 (36 DSP, 15k LUT) and two stages.

## Open questions (most important first)

1. **A against C** turns on the occasion rate: at one occasion per frame C fits on a core and saves
   ~20k LUT; at one per slot only A (or B) fits.  Recommended: A, because it meets the worst case and
   leaves the A53 free.
2. **msg1-FDM > 1**: A's extract can select several occasions' bins from the same FFT output (a
   second pass of the back end); today a second FDM occasion is a second command.
3. **The capture front-end upstream** (`spec.md`, open question 2).
