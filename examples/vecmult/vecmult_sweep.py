"""vecmult_sweep.py — the 16 design points behind ``docs/examples/vecmult/sweep.md``.

The grid is chosen to separate the two BRAM regimes rather than to cover a box uniformly:

* ``vlen = 512``   — banks far shallower than a BRAM18; the LUTRAM corner lives here.
* ``vlen = 1024``  — every bank rounds up to exactly one block, so BRAM should track ``LW``.
* ``vlen = 4096``  — straddles: partition-bound at large ``LW``, data-bound at small.
* ``vlen = 16384`` — banks deeper than a block, so BRAM should be **data-bound** and independent
  of ``LW``.  This column is the one that distinguishes a ceiling law from ``BRAM = LW``.

``vlen`` is the **outer** axis because that is how the regimes are organised: a partial run then
covers whole regimes rather than a slice of each.

Each point is one csynth, and the whole grid measured 890 s end to end::

    python -m examples.vecmult.vecmult_sweep --dry-run    # codegen only, no Vitis
    python -m examples.vecmult.vecmult_sweep
    python -m examples.vecmult.vecmult_sweep --vlen 512 --resume

Everything that is not about *this design* -- resume, incremental save, per-point failure isolation,
progress, the exit code -- belongs to :mod:`waveflow.build.sweep` and is not repeated here.
"""
from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent

from waveflow.build.sweep import ParamGrid, Stage, SweepRunner, sweep_cli  # noqa: E402

from examples.vecmult.vecmult_build import build_vecmult_dag  # noqa: E402

DWIDS = (32, 64, 128, 256)
VLENS = (512, 1024, 4096, 16384)

#: The work-tier platform this sweep accumulates into.  Deliberately **not** the name of a tracked
#: library: a sweep is exploratory and re-runs freely, so it writes to the untracked ``calib/work/``
#: tier and a deliberate publish promotes the result::
#:
#:     waveflow_calib publish examples/vecmult/calib/work/zynq7020_vecmult_sweep \
#:                            examples/vecmult/calib/platforms/zynq7020_vecmult --apply
#:
#: Setting it at all is what makes the sweep produce a **record store** rather than only a summary.
#: Without it ``InspectSynthStep`` attributes the report and has nowhere shared to file it, so the
#: measurements survive only as numbers a human copies into source -- which is exactly how this
#: example's corpus started life.
PLATFORM = "zynq7020_vecmult_sweep"
PLATFORMS_ROOT = HERE / "calib" / "work"
PART = "xc7z020clg484-1"
CLK_FREQ = 100e6

#: ``vlen`` outer, ``dwid`` inner — see the module docstring.
GRID = ParamGrid(vlen=VLENS, dwid=DWIDS)

RUNNER = SweepRunner(dag_factory=build_vecmult_dag, root_dir=HERE,
                     platform=PLATFORM, platforms_root=PLATFORMS_ROOT,
                     part=PART, clk_freq=CLK_FREQ,
                     extra_params={"live_output": False})

#: One stage: synthesize, attribute the report, file the records.  ``--dry-run`` stops at codegen and
#: attaches no platform -- nothing was synthesized, so there is no report to file.
STAGES = [Stage("resources")]
DRY_RUN_STAGES = [Stage("codegen_dut", use_platform=False)]


def points() -> list:
    """The grid as a list of parameter dicts — for callers that want the points alone."""
    return list(GRID)


def main(argv: "list[str] | None" = None) -> int:
    return sweep_cli(RUNNER, GRID, description="Sweep vecmult resource points",
                     stages=STAGES, dry_run_stages=DRY_RUN_STAGES, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
