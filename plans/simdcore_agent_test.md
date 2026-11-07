# Pre-registration: the SIMD core's agent test (`plans/simdcore.md` Stage 7, success criterion 5)

**Registered 2026-11-07, before any agent saw a kernel. Not scored yet** (scored once, after Stage 7's
docs are final). Changing a kernel, its golden or the rule after this date voids the registration:
log the change and its reason below, and score the new set as a new registration.

## The rule

- **The agent** gets only `docs/guide/dsp/simdcore/` (the three pages, as they stand at scoring) and
  the RVV 1.0 specification, plus one kernel's **task text** below. It does not see `examples/`,
  `tests/`, `waveflow/` or this file's goldens. The model and its version are recorded with the score.
- **It writes** one program (GNU assembly text) per kernel.
- **Scoring:** the program runs on the interpreter (`waveflow.simdcore.model.run_job`) with the
  kernel's configuration on **5 seeded random inputs** (seeds 0-4) and must equal the golden in every
  output element (and `a0` where the kernel returns a scalar), status `OK`. If the first attempt
  fails, the agent gets the failure -- the assembler's or interpreter's error message, or "output
  mismatch at element i" -- and one more attempt. **Pass** = first or second attempt.
- **Reported:** passes on the first attempt, passes by the second, and every failure mode by which
  RVV habit or doc gap caused it.
- **Target** (criterion 5): at least 6 of 8 by the second attempt.

## The kernels

Each runs one job on `SimdCoreConfig(L=2, vlen=2048)` with the windows stated; one block of `N`
elements a window (`N` = 64 at `e32`, 128 at `e16`), `a0 = N`. Complex data: a 32-bit element, `re`
bits 15:0, `im` 31:16. `rnu(p, d) = (p + 2^(d-1)) >> d` (round half up), `sat16` / `sat32` clip.

| # | Kernel | Task text given to the agent | Golden |
|---|---|---|---|
| 1 | beamform-2 | Window `X0` (`v1`, in, complex), `X1` (`v2`, in, complex), `Y` (`v3`, out, complex). Complex weights `w0` in `a1`, `w1` in `a2` (packed as the elements). Per part, `y = sat16(t0 + t1)` with `t_j = sat16(rnu(conj(w_j)·x_j, 15))`. | as stated |
| 2 | power-count | Window `X` (`v1`, in, complex, parts in `[−32767, 32767]`). Threshold `T` (32-bit, non-negative) in `a1`. Return in `a0` the number of `i` with `re² + im² > T`. | `sum(xr² + xi² > T)` |
| 3 | rotate | Windows `X` (`v1`, in, complex), `R` (`v2`, in, complex), `Y` (`v3`, out, complex). `y = sat16(rnu(x · r, 15))`, then halved with rounding: `y = rnu(y, 1)` per part. | as stated |
| 4 | normalize-peak | Built with `vdiv=True`. Window `X` (`v1`, in, `e32` real, values in `[-2^20, 2^20)`), `Y` (`v3`, out, `e32` real). `m = max |x_i|` (at least 1); `y_i = (x_i << 10) / m`, division truncating toward zero. | as stated |
| 5 | horner-4 | Window `X` (`v1`, in, `e16` real Q15), `Y` (`v3`, out, `e16`). Coefficients `c0..c4` (Q15) in `a1..a5`. `y = c0 + x(c1 + x(c2 + x(c3 + x·c4)))`, each multiply `vsmul` semantics (Q15, round half up, saturate) and each add saturating. | as stated, step by step |
| 6 | argmin | Window `X` (`v1`, in, `e32` signed). Return in `a0` the first index of the minimum. | `argmin(x)` |
| 7 | soft-limit | Window `X` (`v1`, in, `e16` signed, values in `[−32767, 32767]`), `Y` (`v3`, out, `e16`). `y = x` where `|x| <= A`, else `sign(x)·(A + ((|x| − A) >> 2))`, `A` in `a1` (`0 < A < 2^14`). | as stated |
| 8 | complex-energy-sum | Window `X` (`v1`, in, complex). Return in `a0` `Σ_i rnu(re_i² + im_i², 8)` as a 32-bit wrapping sum. | as stated |

Their goldens and configurations are `tests/simdcore/agent_test/kernels.py` (to write before scoring,
from this table, by someone other than the agent under test).

## Log

| Date | Event |
|---|---|
| 2026-11-07 | Registered (8 kernels, the rule, the target) |
