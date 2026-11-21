# detect -- `prach/prach.py::Detect`, `src/prach_detect_task.h`

**Job.** Per root: the power-delay profile over antennas, each shift's window peak, the threshold
test; then the answer (`golden.power`, `golden.detect_root`).  The last stage: one `Resp` and one
`Status` per command, errors included.

| port | kind | carries |
|---|---|---|
| `s_cmd` | framed, 128 <- correlate | `CorHdr` |
| `s_fft` | unframed, 200 <- fft256 | `n_roots x n_ant x 64` words of 4 `ap_fixed<25,11>` |
| `m_out` | framed, 128, boundary | `Resp(tx_id, n_det, exp)`, then the `DetRec`s as one burst (none when 0) |
| `m_status` | framed, 128, boundary | `Status(tx_id, status)` |

**Datapath** (decision 17).  `q = sat18(rnd(z / 2^6))`, `P[n] (+)= qr^2 + qi^2` (exact, 38 bits); a
noise mask set; per shift the window `[n_start - 4, ... )` mod 256, the first maximum, the mask
cleared +-2 round it, the root's strongest peak kept; the noise `S` (46 bits) = the profile where the
mask is set, `N` (9 bits) its count; per window `peak x N x 2^8 > thr_q8 x S` (64 bits) and
`40 x peak > max` (shift-add).  Detections are buffered (at most 64) and written at the end, so `Resp`
can say how many; each `DetRec` carries `S` and `N`.

**Rate.** ~640 cycles a root (256 the profile, 64 the mask, ~290 the windows, 64 the noise, ~10 the
decisions): ~7,000 a B4 job, under the input's 12,290.  **Resources** ~13 DSP; the profile 4 x 64 x 38
bits, the mask 256 bits; **unknown -- csynth probe `detect_csynth`**.
