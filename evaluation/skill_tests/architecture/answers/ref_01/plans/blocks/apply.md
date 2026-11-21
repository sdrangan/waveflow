# blocks/apply.md -- `apply[0..3]`: x_hat = W Y (alternatives A and B)

- **Library block**: `SystolicEngine` (`dsp/systolic/systolic_engine.md`), four instances; engine `j`
  takes the PRBs with `prb mod 4 == j`.
- **Configuration**: `R 4, C 8, L 4, Mmax 8, Kmax 32, Pmax 1, Nmax 32, form 4`, `word_bits 128`,
  `lane_bits 16`; `a` = `W` `ap_fixed<16,2>`, `b` = `Y` `ap_fixed<16,1>`, `c` = `x_hat` `ap_fixed<16,3>`
  (rounded and saturated once, as `golden.fixed_detect` does).
- **Ports**: `s_in` (128-bit): an `EngineCmd {tx_id, op MUL, m = U, kp = 32, n = 24, npanels 1, nb 7,
  keep_a 1}`, then `W` (`U × 32`), then 7 blocks of `Y` (`32 × 24`). `s_out`: per block an `EngineResp`
  and `x_hat` (`U × 24`). `m_status`: one `EngineStatus` per command.
- **Rate**: one command per 4 filter groups' worth of time: 1,537 cycles a command against 1,831
  available (84 %). **II** (a command): 1,537 cycles; **latency**: about the same.
- **Resources** (each): 154 DSP, 37,301 LUT, 44,524 FF, 56 BRAM18 [rm, UNCALIBRATED, `Kmax 32`
  extrapolated]: **one csynth probe** of this configuration settles LUT and FF.
- **`U < 8`**: `m` must be a multiple of `R = 4`: pad `W` with zero rows to 4 or 8 (`x_hat` rows
  dropped by `coll`); costs nothing (input-bound).
- **Frame**: none to build -- a library block; its harness is `examples/systolic/engine_harness.py`.
