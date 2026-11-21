# correlate -- `prach/prach.py::Correlate`, `src/prach_correlate_task.h`

**Job.** Per root and antenna, `Z = conj(Y) R_u / 2` (rounded, saturated to int16), zero-padded to a
256-point frame for fft256 (`golden.correlate`).

| port | kind | carries |
|---|---|---|
| `s_y` | framed, 128 <- extract | `ExtHdr`, roots, Y |
| `m_cmd` | framed, 128 -> detect | `CorHdr` -- written first, always (errors included) |
| `m_fft` | unframed, 128 -> fft256 | `n_roots x n_ant x 64` words |

**The reference** is generated, not stored per root: `R_u(k) = ROM[u m (m+1) mod 278]`,
`m = u^-1 k mod 139`; the ROM is 278 Q15 cos/sin pairs (`prach_rom.h`, generated from
`golden.phase_rom`), one copy per lane.  Software supplies `u^-1` with `u`; front checks the pair.
Rejected: a ROM of all 138 roots' sequences (19,182 entries, ~19 BRAM18) and a host-loaded table (a
second configuration path to keep in order) -- decision 8.

**Rate.** 1 word / cycle out: 2,816 cycles a B4 4-antenna job of 11 roots; 16,384 at N_CS = 0
(64 roots).  **Resources** ~16 DSP for the products + the two constant modulos a lane (a guess: ~8);
ROM + Y banks ~6 BRAM18; **unknown -- csynth probe `correlate_csynth`**.
