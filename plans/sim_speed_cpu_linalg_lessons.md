# Lessons: simulation speed of the processor model and the linalg units

Lessons learned while executing [`plans/sim_speed_cpu_linalg.md`](sim_speed_cpu_linalg.md),
newest last.

- **2026-10-19 (step 1): activate the venv, don't just call its Python.** `.venv/bin/python` runs
  without what `.venv/bin/activate` exports on this machine. That is `XILINX_VIVADO=/tools/Xilinx/Vivado/2024.1`,
  and `.venv/xsi_compat/` on `LD_LIBRARY_PATH`: a symlink so that the 2025.1 name the XSI harness loads
  (`libxv_simulator_kernel.so`) resolves to 2024.1's `librdi_simulator_kernel.so`. Without them, every XSI gate errors
  (`run.sh: cannot locate Vivado`). The Python toolchain finds Vitis and Vivado by itself, which
  hides the problem until an RTL run. The fact was already recorded in
  `plans/mimo_cg/mimo_cg_paper_sims.md` §6. Read earlier plans' environment sections before writing a
  new one.
- **2026-10-19 (step 1): pytest's totals line.** `pyproject.toml` sets `addopts = "-q"`, so adding
  `-q` on the command line gives `-qq`, which drops the "N passed, M failed" line. Pass `-rfEs` alone.
- **2026-10-19 (step 1): plans are linted too.** `tests/docs/test_markdown_integrity.py` scans every
  Markdown file, `plans/` included. A table cell with `Σ` immediately before a backtick (no space)
  failed it. Run `pytest tests/docs/test_markdown_integrity.py` after editing a plan.
- **2026-10-19 (step 2): a `tail` at the end of a check hides its failure.** `pytest ... | tail -1 && git commit`
  committed even though the lint failed, because the pipeline's status is `tail`'s. Gate commits on
  `pytest`'s own exit status (`set -o pipefail`, or no pipe). The same no-space-before-backtick slip
  (`Σ` before a backtick) went in twice; write "the sum of" in plans.
- **2026-10-19 (step 1): the XSI suite writes into tracked files.** A `pytest -m xsi` run modified
  `examples/interleaver/xsi/rtl_mem_r_stream.f` in the working tree. Stage files by name, never
  `git add -A`, while it runs, and inspect such files after it.
- **2026-10-19 (step 4): add a measurement before the results are written, not after.** A `--gem5`
  block inserted after `RUNTIME.write_text` ran for 2.5 min and saved nothing. Keep the write as the
  last statement before the exit status, and check the key in the JSON after every new fidelity.
- **2026-10-19 (step 4): what "N× faster than gem5" means depends on gem5's mode.** In `mode=total`,
  gem5 spends ~0.47 ms of host time per scheduler operation inside the measured region, but ~11 ms per
  operation end to end (parsing, a warm-up pass, printing counters, plus 5 s of container start). In
  per-op marker mode, the stats dumps dominate (657 s for 410 ops). The same pysim is ~15×, ~340× or
  ~72,000× faster depending on which of these it is compared with, so always name the comparison.
- **2026-10-19 (step 8): an XSI testbench's idle cycles are cheap, so pro-rating by cycles misleads.** The
  linalg benches run exactly `n_cycles`. Sizing it at 1.25× the pysim's end time left 21-35 % idle
  cycles after the last reply. Pro-rating the simulate time by cycles then under-stated the RTL by
  ~15 %: cutting 16 % of the cycles saved only 3.5 % of the time. Size `n_cycles` tightly (the
  pysim is within 1 % of the RTL here) and let `check_xsi` catch a run cut short.
- **2026-10-19 (step 10): a ratio stopping rule penalizes making the model itself faster.** The rule
  "stop at pysim ≤ 2× untimed" treats the untimed model as fixed. Memoizing the CPU model's prices
  sped up the untimed sum 3.9× and the pysim 1.8×, so the ratio went from 2.7× to 5.7× while both got
  faster. Report the overhead (pysim − untimed, per unit) and the absolute times, not only the ratio.
- **2026-10-19 (step 10): validate an A/B harness with a null run first.** With A and B the same code,
  B/A came out 0.99-1.00. That set the noise floor (~1 %) under the 3 % "helps" bar, and showed each
  side imported its own tree (`python -m`/`-c` put the cwd first on `sys.path`).
- **2026-10-19 (step 17): a commit on its own line is not gated by the chain above it.** `ruff ... &&
  pytest ... && python3 <<EOF (plan update)` failed at ruff, which skipped the tests and the plan
  update. But `git add ... && git commit` on the next line still ran, and committed a lint error
  without its log row. Run the checks, read their status, then commit in a separate step.
