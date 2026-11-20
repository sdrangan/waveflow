# Lab conventions

These hold for every project in our group.

- Python 3.10+, type hints on public functions, numpy for array work.
- Tests with pytest, in `tests/`; one test file per module. Run `pytest -q` before saying a change
  is done.
- Keep modules small: one class per file unless the classes are tiny.
- Plots with matplotlib; save figures as PNG in `figs/`, never `plt.show()` in scripts.
- Never commit data files over 1 MB; put them in `data/` (gitignored).
- Write a short docstring at the top of each module saying what it is for.
