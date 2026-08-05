"""fir_block_sweep.py — the B2 parameter sweep: one csynth per design point, records accumulated.

Phase B2 of ``plans/resource_model.md``.  Drives the ``fir_block`` DAG through ``resources`` once per
point in a ``ntap x samp_w x realization`` grid, so the module store ends up holding a real corpus for
the priors (D1) and the composition fit (E1) to be tested against.

**Writes to the work tier.**  The sweep churns and its numbers are unproven, so it lands in an
untracked ``calib/work/`` platform under its own name.  Using a tracked library's name would make
``Platform.resolve`` find that directory through its fallbacks and write into it, which only
``publish_calib`` may do.  Promotion is then a deliberate act::

    waveflow_calib publish examples/fir_block/calib/work/zynq7020_fir_sweep \
                           examples/fir_block/calib/platforms/zynq7020_bfm_100mhz --apply

Usage::

    python -m examples.fir_block.fir_block_sweep --dry-run     # elaborate + codegen only, no Vitis
    python -m examples.fir_block.fir_block_sweep               # the full sweep
    python -m examples.fir_block.fir_block_sweep --resume      # continue an interrupted one
    python -m examples.fir_block.fir_block_sweep --ntap 8 16 --realization unroll

Never silently drops a point, writes incrementally, and resumes -- but none of that is written here.
It belongs to :mod:`waveflow.build.sweep`, which owns it for every sweep rather than for this one:
a csynth that fails is recorded as a failure with its error, because a sweep that quietly covered 19
of 24 points and reported 24 would put a hole in the fitted region exactly where an agent would later
be told it was interpolating.
"""
from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent

from waveflow.build.sweep import ParamGrid, Stage, SweepRunner, sweep_cli  # noqa: E402

from examples.fir_block.fir_block import DEFAULT_SAMP_I, MEM_DW  # noqa: E402
from examples.fir_block.fir_block_build import build_fir_block_dag  # noqa: E402

NTAPS = (8, 16, 32)
SAMP_WS = (8, 12, 16, 24)
REALIZATIONS = (False, True)

#: Memory-word widths.  Held at one value by default: ``mem_dwidth`` is the one knob that changes the
#: *boundary* (adapter and FIFO widths) as well as the modules, so sweeping it invalidates every module
#: key at once.  It belongs in a coarse outer loop, not the inner grid — but varying it deliberately is
#: how the interface term's model is tested, hence the CLI override.
MEM_DWS = (MEM_DW,)

PLATFORM = "zynq7020_fir_sweep"
PLATFORMS_ROOT = HERE / "calib" / "work"
PART = "xc7z020clg484-1"
CLK_FREQ = 100e6                      # the 10 ns period the generated TCL creates

#: ``samp_i`` is a single-value axis: a constant this design holds fixed, carried into every point
#: without branching and left out of labels, where it could only pad.
GRID = ParamGrid(ntap=NTAPS, samp_w=SAMP_WS, unroll_lane=REALIZATIONS,
                 mem_dwidth=MEM_DWS, samp_i=(DEFAULT_SAMP_I,))

RUNNER = SweepRunner(dag_factory=build_fir_block_dag, root_dir=HERE,
                     platform=PLATFORM, platforms_root=PLATFORMS_ROOT,
                     part=PART, clk_freq=CLK_FREQ,
                     extra_params={"live_output": False})

STAGES = [Stage("resources")]
DRY_RUN_STAGES = [Stage("codegen_dut", use_platform=False)]

#: ``--realization`` rather than ``--unroll-lane 0 1``: the axis is boolean, and a named choice reads
#: better than a pair of integers.  The mapping stays here rather than in the framework because
#: "serial" and "unroll" are *this design's* vocabulary for what the flag selects.
_REALIZATIONS = {"serial": (False,), "unroll": (True,), "both": REALIZATIONS}


def points() -> list:
    """The grid as a list of parameter dicts — for callers that want the points alone."""
    return list(GRID)


def main(argv: "list[str] | None" = None) -> int:
    return sweep_cli(
        RUNNER, GRID, description="Sweep fir_block resource points",
        stages=STAGES, dry_run_stages=DRY_RUN_STAGES, argv=argv,
        extra_args=[(("--realization",),
                     {"choices": tuple(_REALIZATIONS), "default": "both",
                      "help": "which kernel realization(s) to sweep"})],
        grid_from_args=lambda g, a: g.subset(unroll_lane=_REALIZATIONS[a.realization]))


if __name__ == "__main__":
    raise SystemExit(main())
