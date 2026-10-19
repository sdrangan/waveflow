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
