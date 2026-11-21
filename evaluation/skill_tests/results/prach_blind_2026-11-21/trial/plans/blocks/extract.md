# extract -- `prach/prach.py::Extract`, `src/prach_extract_task.h`

**Job.** The 139 PRACH bins of every window, summed over the repetitions, scaled by one block
exponent per occasion to int16 (`golden.extract_combine`).

| port | kind | carries |
|---|---|---|
| `s_cmd` | framed, 128 <- front | `FwdHdr`, roots, `FwdTrl` |
| `s_fft` | unframed, 216 <- fft1024 | `nframes x 256` words of 4 bins `ap_fixed<27,13>` |
| `m_y` | framed, 128 -> correlate | `ExtHdr(.., exp, code)`; when OK the roots and Y (n_ant x 140 bins, 4 a word) |

**Datapath.**  A word a cycle; the four lanes rotated by `k_start mod 4` so lane `m` always writes bank
`m` of a cyclic-4 accumulator `ap_fixed<31,17>` (12 repetitions of 27-bit bins: exact).  The first
repetition writes, the rest add; the last repetition's pass tracks `max |A|`.  Then `e` (16 compares)
and `Y = A >> e` assigned to `ap_fixed<16,2,AP_RND,AP_SAT>`.

**Why a stage of its own**: the exponent needs the whole job before the first Y can leave.

**Rate.** 1 word / cycle in (12,288 a job), 149 words out.  **Resources** 0 DSP; accumulators 2 x 4 x 4 x
35 x 31 bits (BRAM or LUTRAM); LUT **unknown -- csynth probe `extract_csynth`** (and whether `FRAMES`
reaches II = 1 with the rotated banks).
