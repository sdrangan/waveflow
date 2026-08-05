"""mem_copy_sweep.py — the writer's timing residual, measured rather than transcribed.

P3b of ``plans/sweep_runner.md``: the sweep that proves :class:`~waveflow.build.sweep.Stage` serves
the **timing** axis and not only the resource one.

What it fits is the writer's own *control* cost.  Each point runs the RTL through XSI and a pysim
that already charges the platform's bus law, and the residual between the two is what is left after
the bus term is accounted for -- see :mod:`waveflow.calib.timing_model`.

**Every axis here is a workload axis.**  ``n_words`` is a runtime field of the copy command, so the
hardware is identical at every point: one csynth serves the whole sweep.  That is why the stage
names the steps to force instead of forcing everything -- see :class:`~waveflow.build.sweep.Stage`.
It is also why this sweep is minutes rather than hours: ~20 s a point against ~45 s of one-time
synthesis.

    python -m examples.mem_copy.mem_copy_sweep --dry-run    # pysim only, no toolchain
    python -m examples.mem_copy.mem_copy_sweep              # the three points
    python -m examples.mem_copy.mem_copy_sweep --n-words 128 512 --resume

The grid replaces the constants ``calibrate_platform.py`` carries::

    RTL_SPAN = {128: 183.0, 512: 615.0}

Those are real measurements, typed in by hand because the automated path filed nothing -- the trace
manifest named a task by its function while the timing model keyed itself by its configuration, so
the two corpora never joined (fixed in ``_task_trace``).  A sweep that reproduces 183 and 615 is what
lets them go.
"""
from __future__ import annotations

from pathlib import Path

HERE = Path(__file__).resolve().parent

from waveflow.build.sweep import ParamGrid, Stage, SweepRunner, sweep_cli  # noqa: E402

from examples.mem_copy.mem_copy_build import CLK_FREQ, PART, build_mem_copy_dag  # noqa: E402

#: Job sizes.  128 and 512 are the two ``calibrate_platform.py`` measured by hand -- kept so the
#: sweep can be checked against them -- and 256 widens the span a little.
#:
#: 1024 was held out for a while on the belief that its RTL stalled.  It never did: ``rtlsim`` did
#: not depend on the step that GENERATES the harness, so every point ran under the committed gate's
#: 3400-cycle bound and the large ones were simply cut off mid-job.  With that edge in place
#: (``RtlSimStep.tb_artifact``) each point gets its own ``xsi_run_cycles`` bound and 1024 completes.
N_WORDS = (128, 256, 512, 1024)

#: Jobs per point, the same at every size.  It could be a second axis -- more small jobs sharpen a
#: median -- but the grid is a cartesian product, so pairing one job count to each size is not
#: something it can express, and four firings is already enough for the median to be stable.
NUM_CMDS = 4

PLATFORM = "zynq7020_bfm_sweep"
PLATFORMS_ROOT = HERE / "calib" / "work"

GRID = ParamGrid(n_words=N_WORDS, _workload=("n_words",))

RUNNER = SweepRunner(dag_factory=build_mem_copy_dag, root_dir=HERE,
                     platform=PLATFORM, platforms_root=PLATFORMS_ROOT,
                     part=PART, clk_freq=CLK_FREQ,
                     extra_params={"live_output": False, "num_cmds": NUM_CMDS})

#: The steps a workload point reaches.  `trace_manifest` is in the list because the scenario changes
#: the harness, `rtlsim` because it writes this point's vectors and runs them; everything downstream
#: (extract_bursts, collect_timing, fit_timing) re-runs by cascade once their inputs are newer.
#: `codegen_dut` and `csynth` are deliberately absent -- the DUT is scenario-independent.
_TIMING_STEPS = ["pysim", "codegen_tb", "trace_manifest", "rtlsim"]

#: Two passes, and the order is load-bearing rather than tidy.
#:
#: The bus law is a property of the **platform**, measured off the m_axi ports, and it needs at least
#: two distinct sizes before it can be fitted at all.  The writer's residual is what is left *after*
#: the bus is charged -- so a component pass running before the law exists rolls the transfer cost
#: into the component's own number, and one running while the law is still growing gives every point
#: a different meaning.  ``barrier=True`` finishes the bus across the whole grid first.
#:
#: The second pass re-runs ``rtlsim`` rather than reusing the first pass's waveform: the trace
#: artifacts are single-slot (``results/mem_copy_timing.json``), so after the barrier what is on disk
#: belongs to the *last* point.  That doubles the RTL time, which at ~16 s a point is the cheaper
#: half of being correct.
#: The collect pass stops at ``collect_timing``, short of the fit, and that is the whole point.
#:
#: ``fit_timing`` writes ``params.json``; the next point's pysim loads it and charges the predicted
#: delay.  ``residual = rtl - pysim + current_dly`` is meant to undo that, but it assumes the delay is
#: **additive in the measured span**, and in a back-pressured pipeline it is not.  Measured, fitting
#: after every point: residuals of 23, 16, 0 for a cost that is flat, with the n=256 pysim overshooting
#: the RTL outright (334 vs 327) because it charged a delay fitted at n=128.
#:
#: Collecting every point against one frozen model, then fitting once, removes the coupling: no
#: params.json is written while the corpus is being built, so every point is measured from the same
#: baseline.
STAGES = [Stage("calib_bus", name="bus", barrier=True, force=_TIMING_STEPS),
          Stage("collect_timing", name="collect", barrier=True, force=_TIMING_STEPS),
          Stage("fit_timing", name="fit", force=["fit_timing"])]

#: A dry run stops at the pysim: it needs no toolchain, and it is where a scenario that does not fit
#: the arena fails.  No platform, because nothing was measured.
DRY_RUN_STAGES = [Stage("pysim", use_platform=False, force=["pysim"])]


def main(argv: "list[str] | None" = None) -> int:
    return sweep_cli(RUNNER, GRID, description="Sweep mem_copy writer timing points",
                     stages=STAGES, dry_run_stages=DRY_RUN_STAGES, argv=argv)


if __name__ == "__main__":
    raise SystemExit(main())
