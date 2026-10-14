# Lessons: simulations for the LTBF CG design-space exploration

Plan: `plans/mimo_cg/ltbf_cg_dse.md`. Lessons from the earlier detector study, which apply here
too, are in `plans/mimo_cg/mimo_cg_lessons.md` (background-task limits, BLAS oversubscription,
csynth memory, `.gitignore` swallowing `*.json`/`*.xml`).

## Planning (2026-10-13)

- **Read the paper's code and its deviation log, not only its text.** The text describes one
  sector of 10 UEs over 100 MHz; the code simulated three sectors whose covariance sums all 30
  UEs (`Ĥα` up to 240 columns) over 1.44 MHz, where the Denver channels are rank one. The plan's
  sizes, budgets and one of its findings changed when the independent review read
  `docs/known_deviations.md`.
- **A 48-hour deadline turns every multi-hour job into a detached, resumable one.** The harness
  kills background tasks after 2 h at most; the earlier study lost an 8 h sweep that way.

## Execution (2026-10-13 to 2026-10-14)

- **Compare a fixed-point model against the float one on real data, not only on synthetic
  links.** The 24-bit check passed on an i.i.d. synthetic link and failed on the Denver drops at
  t ≥ 8: static formats lose relative precision as the CG residual shrinks. A synthetic test can
  prove the plumbing; it cannot prove the accuracy claim.
- **The paper's algorithm includes its stop rule.** `cg_solve_complex` leaves a column at
  `rᴴr < 1e-10`; the first float reference ran every column for the full t.
- **Read the eigenvalue spectrum before reporting a rank.** With a noiseless SRS estimate `Mᵢ` is
  rank one here, so an r = 2 SINR depends on a round-off eigenvector.
- **Ragged sizes are where a tiled HLS body breaks.** Every test used sizes that divided the tile;
  the reviewer found that real sector populations (odd UE counts) silently dropped columns. Pad on
  the host (exact for zero columns of H and B) and make pysim refuse unpadded jobs.
- **csynth's latency is a structural count, a few cycles pessimistic per tile.** Three XSI
  anchors at two sizes separated a fixed per-tile correction (3.74 cycles) from a ratio; one size
  could not have.
- **Vitis maps multiplies per width.** DSP is not monotone in W (W10 packs two per DSP, W20 takes
  two, W24 moves some to fabric); a DSP model with one rule for all widths missed by 60 %. The
  packing logic lands in LUTs, which the LUT model then under-predicts at W10 — and that alone
  cost AC9 its LUT decisions.
- **Freeze the model before the held-out data lands, and hash it.** The review arrived while the
  queue was producing held-out reports; the restructured model was committed and hashed before
  any held-out report was read.
- **Profile drops must be excluded from the scored statistics.** Formats profiled on drops 1–10
  cannot saturate there; scoring on 11–100 is the honest number.
- **Check that a thread count does not change results before relying on it.** A `cmp` of one job
  at 1 and 2 BLAS threads made restarting the sweep single-threaded (Rule 8) safe for AC4.
- **A session can end while detached jobs run.** The sweep, the queue and every later step ran
  under `setsid nohup` with logs and done markers, so a 7 h session gap lost nothing.
