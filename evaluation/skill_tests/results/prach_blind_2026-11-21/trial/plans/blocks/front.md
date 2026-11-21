# front -- `prach/prach.py::Front`, `src/prach_front_task.h`

**Job.** Check the command and the roots; forward them; copy every window to fft1024 a word a cycle.

| port | kind | carries |
|---|---|---|
| `s_in` | framed, 128 | `PrachCmd`, roots (U8, 16 a word), `n_sym x n_ant` windows of 256 words |
| `m_cmd` | framed, 128 -> extract | `FwdHdr(.., nframes)`, roots (when `nframes > 0`), `FwdTrl(tx_id, code)` |
| `m_fft` | unframed, 128 -> fft1024 | `nframes x 256` words |

**Rules.**  Errors found before a window (BAD_PARAM, BAD_ROOT, short roots) -> `nframes = 0`, nothing to
the FFT, `FwdTrl(code)`, halt.  An early TLAST in a window -> the rest of the job's words are zeros
(the FFT counts words, it has no TLAST), `FwdTrl(TLAST_EARLY)`, halt.  No TLAST on the command's
word, the last root word or a window's word 255 -> `TLAST_LATE`, the same path (the excess is never
read; decision 20).  The command paces the
pipeline: front reads a job's windows only after it has written that job's `FwdHdr`.

**Rate.** 1 word / cycle on `WIN` (II = 1): 12,290 cycles a B4 4-antenna job.  **Latency** a few cycles.
**Resources** 0 DSP, 0 BRAM (a 128-byte root buffer); LUT/FF **unknown -- csynth probe `front_csynth`**.
**Frame** `stream_pipeline` (a stage); verified alone by `harness(Front)` on the three scenarios.
