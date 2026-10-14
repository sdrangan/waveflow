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
