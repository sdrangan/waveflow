"""g1_prereg.py -- the grid G1, pre-registered before any of its builds run (``plans/dse_cg.md`` step 4).

Committed before ``grid.py`` measures a point, and not edited after: a change is a new
pre-registration with a new date.  What the grid is, what is measured at each point, how pysim's
predictions are scored against it, and which points an integration term may be fitted on.  The
objectives the agents will be given (``prereg.py``, plan step 7) are fixed separately, before any G1
result is examined; their optima are computed from G1.

Python rather than JSON: the repository's ``.gitignore`` ignores ``*.json``.
"""

from __future__ import annotations

#: The date of this pre-registration.
DATE = "2026-11-07"

#: The space: ``ltbf_cg_mem.GRID`` as it stands at this date, ``{name: (kind, values, default)}``.
#: The four calibrated systolic shapes restrict (``R``, ``C``): 384 legal designs.  The resizing rule
#: was applied once to step 2c's measured costs (169-196 slot-hours, x 1.3 = 220-255 against 336):
#: the grid stands.
GRID = {
    "R": ("design", (2, 4, 8), 4),
    "C": ("design", (4, 8), 8),
    "W": ("design", (12, 16), 16),
    "form": ("design", ("factored", "explicit"), "factored"),
    "L": ("design", (2, 4), 4),
    "banks": ("design", (1, 2), 1),
    "mem_rate": ("design", (2, 1), 2),
    "cg_window": ("design", (1, 2, 4), 2),
    "t": ("workload", (2, 3), 3),
    "target": ("workload", ("-6_14", "-6_3"), "-6_14"),
}

#: What is measured at every point (``grid.py``), Vitis HLS / Vivado 2025.1 on the lab machine,
#: ``xczu48dr-ffvg1517-2-e`` at 4 ns: csynth of each root's kernels, Vivado out-of-context synthesis
#: of each built part, and the long-term update at RTL through XSI at each ``t`` (768 runs) -- each
#: with the tool time it took.  ``golden`` (8 accuracy tables) and ``pysim`` are computed over the
#: grid when it is scored.
MEASURED = ("hls_estimate", "synth", "rtlsim")
TOOLS = "Vitis HLS / Vivado 2025.1"
PART, PERIOD_NS = "xczu48dr-ffvg1517-2-e", 4.0

#: How pysim's predictions are scored against G1, once, over every point outside
#: :data:`INTEGRATION_POINTS`: per built part, DSP and BRAM exact; LUT and FF within 5% mean absolute
#: relative error; the long-term update's cycles within 5% mean; and of every objective's pick by
#: pysim (the best design pysim predicts), at least 90% within 10% of the measured optimum.  A miss
#: stops at step 4 and goes to the user.
SCORING = {
    "dsp": {"per": "part", "rule": "exact"},
    "bram": {"per": "part", "rule": "exact"},
    "lut": {"per": "part", "rule": "mean_abs_rel", "max": 0.05},
    "ff": {"per": "part", "rule": "mean_abs_rel", "max": 0.05},
    "cycles": {"per": "run", "rule": "mean_abs_rel", "max": 0.05},
    "picks": {"per": "objective", "rule": "within_of_optimum", "within": 0.10, "share": 0.90},
}

#: The points a system-level integration term (the wrappers' glue, the hub and the crossbar, which
#: the units' models do not price) may be fitted on, chosen before G1 is measured and excluded from
#: scoring: step 2c's six (``points.POINTS``), already measured.
INTEGRATION_POINTS = ("defaults", "smallest", "largest", "R_changed", "sim_only", "workload_only")
