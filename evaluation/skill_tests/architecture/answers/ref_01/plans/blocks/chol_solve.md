# blocks/chol_solve.md -- `chol_solve` (alternative B): W from G and H

- **Built: no library block does a direct solve** (`systems/library.md`, *What no block covers*).
- **Job**, per filter group: `G += sigma2 I`; Cholesky `G = L L^H` (`U × U`); forward `L Z = H^H`; back
  `L^H W = Z` (32 right-hand sides); `W` out (`U × 32`), with the group's `tx_id`.
- **Ports** (command-response, `systems/command_response.md`): `s_in` (128-bit): a `SolveCmd {tx_id,
  prb, U}`, then `G` (`U × U`), `sigma2`, `H` (`32 × U`). `m_out`: `SolveResp {tx_id}` + `W`.
  `m_status`: `Status {tx_id, OK | NOT_PD}` -- a non-positive pivot (only from rounding: `G` is
  positive definite in exact arithmetic) is reported, and the error is fatal.
- **Rate**: a filter group every 458 cycles; two instances alternate, so each has 916.
- **II**: the triangular solves at II = 1 over (column, row) steps, a `U`-lane dot a step: 512 cycles
  [lane loop]. The factorization: 8 square roots and 36 divides on a serial chain: **unknown -- csynth
  probe**; it must overlap the previous group's solves (double-buffer `L`).
- **Resources**: **unknown -- csynth probe** at `U = 8`, `B = 32`, 16-bit. The budget: two instances
  under ~110k LUT.
- **Golden**: `golden.mmse_filter` (floating point); its fixed-point twin is still to write (formats of
  `G`, `L`, `1/L_ii`).
- **Frame**: `stream_kernel` (one kernel, streams in and out).
