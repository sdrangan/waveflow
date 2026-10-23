"""calib.py — the vector unit's calibration (``plans/vecunit_new.md``, Stage 7): its execute timing
fitted at RTL, and a resource model fitted over csynth runs.

**Pre-registered (rule 5).**  The sets below were committed before any calibration run; the held-out
sets are scored once.

**Timing.**  The execute task's time for a job is linear in counts its program gives before it runs
(:func:`waveflow.vecunit.unit.job_features`): instruction words, passes, runs, run elements, scalar
runs and instructions, divide runs, control instructions, and a constant a job.  pysim charges each
item it issues from the same coefficients (:class:`waveflow.vecunit.unit.VecTiming`), so a job's
outputs leave at the right times inside it, not only at its end.  At RTL a job's execute time is the
interval between its status and the previous job's (``m_status``), on jobs that keep the execute task
busy -- small blocks for the random jobs, so the loader is never what it waits for.  The fit is a
non-negative least squares; the held-out score is each held-out job's interval, pysim (with the fit)
against RTL, end to end -- the matched filter's included, whose loader binds.

**Resources.**  csynth's estimate of the unit (LUT, FF, DSP, BRAM) over the configurations below,
linear in features of the configuration (:func:`resource_features`); the held-out configurations are
scored once.

Every set uses formats that keep every product and the union datapath moderate: the Stage 6 record
has a format mix whose fused body csynth cannot build.
"""

from __future__ import annotations

#: Execute-timing jobs on the CG configuration (``examples.vecunit_cg.cg_unit.CgVecUnit``):
#: ``random`` random jobs (seeded) over every instruction form with blocks of at most ``n_max``
#: columns and the pass counts ``npass`` cycled, then CG ``solves`` ``(k, n, nit, seed)``.
CG_JOBS = {
    "calib": {"random": 12, "seed": 21, "npass": (8, 16, 24, 32), "n_max": 8,
              "solves": [(8, 32, 1, 11), (8, 32, 2, 12), (4, 16, 3, 13), (8, 8, 5, 14),
                         (2, 32, 2, 15), (8, 32, 4, 16), (5, 13, 3, 17), (8, 24, 2, 18)]},
    "holdout": {"random": 6, "seed": 99, "npass": (12, 20, 28), "n_max": 8,
                "solves": [(8, 32, 6, 31), (3, 12, 4, 32)]},
}

#: The held-out timing scenarios: ``(unit, workload)`` -- the two above's ``holdout``, and the steady
#: workloads Stage 6 measured (four ``8 x 32`` CG solves; four matched-filter frames at ``VLMAX`` 64).
TIMING_HOLDOUT = (("cg", "holdout"), ("cg", "steady"), ("mf", "steady"))

#: Resource-model configurations: ``(L, NG, S, slot capabilities, formats)``, formats ``"cg"`` (the
#: CG unit's) or ``"mf"`` (the matched filter's, 16 bits); ``"all"`` capabilities, or ``"mul1"``: only
#: slot 0 multiplies.
RESOURCE_FIT = (
    (4, 32, 3, "all", "cg"), (2, 32, 3, "all", "cg"), (8, 32, 3, "all", "cg"),
    (4, 16, 3, "all", "cg"), (4, 64, 3, "all", "cg"), (4, 32, 2, "all", "cg"),
    (4, 32, 3, "mul1", "cg"), (4, 8, 3, "all", "mf"), (2, 8, 3, "all", "mf"),
    (4, 16, 2, "all", "mf"),
)
RESOURCE_HOLDOUT = ((8, 16, 2, "all", "cg"), (4, 8, 3, "mul1", "mf"), (2, 16, 3, "all", "cg"))

#: Held-out scores the plan asks to report (systolic plan's targets): mean and worst relative error.
TARGET_MEAN, TARGET_WORST = 0.03, 0.06
