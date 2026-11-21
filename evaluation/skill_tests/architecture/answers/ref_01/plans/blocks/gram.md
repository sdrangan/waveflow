# blocks/gram.md -- `gram` (B) / `gram_cg_mm` (A): G = H^H H + sigma2 I

- **Library block**: `SystolicEngine` at the centre (`R 4, C 8, L 4, Mmax 8, Kmax 8, Pmax 4, Nmax 32`,
  128-bit), one instance.
- **Job**: `MUL_AH` with `A = H` (`32 × U`, four panels of 8), `B = H`: `G = H^H H` (`U × U`). Adding
  `sigma2` to the diagonal happens after (in `chol_solve` for B, in the CG program for A): the engine
  does products only.
- **A only**: also each CG iteration's `S = G P` (`8 × 8 × 32`), five a filter group.
- **Rate**: B, 169 cycles a filter group against 458 (37 %); A, 639 cycles a solve against 3,663 at 8
  PRBs a filter.
- **Resources**: 154 DSP, 37,690 LUT [rm, UNCALIBRATED].
- **Formats**: `H` `ap_fixed<16,1>`; `G`'s format to be chosen with `chol_solve`'s golden (open
  question 3 of `spec.md`).
- **Frame**: none to build (library block).
