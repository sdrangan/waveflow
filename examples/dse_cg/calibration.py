"""calibration.py -- what the Waveflow arm is charged for its calibration (plan ``plans/dse_cg.md``,
*Fairness rules* 2): the tool time of every run whose result a model of this system's pysim reads or
was fitted and scored on, at Vitis HLS / Vivado 2025.1 on the lab machine.

``score.summarize(calibration_s=CALIBRATION_S)`` adds it to each Waveflow run's time to target up front
(the headline) and amortized over the Waveflow runs.  Fixed here before any scored run.

**Charged:** the vector unit's resource builds and timing catalogues, the systolic engine's resource
sweep and timing points, the A53 platform and the LTBF kernels on it, the memory's traced device runs,
and the six integration points (step 2c), on which the integration term is fitted -- and, by the
user's decision of 2026-11-12 (the conservative charge, 10.6 h rather than 8.0 h), the systolic and CG
units' calibration (``SystolicUnit``, ``CgVectorUnit``, now retired), which ran in the same campaign
although this system's models do not read it.  **Not charged, listed for the reader:** the toolchain
gates and simulation-speed runs (no model reads them), and the system's development runs (the step
1a / 1f gates and the live gate), which built and checked the framework rather than measured a model's
input.

Each item's seconds come from the run's own record where one exists (a G1-style row's ``seconds`` or
``build_s + simulate_s``), else from the campaign log's start and end lines (one job at a time), else
from its build directory's file times (each build's first file to its result); the sources are the
lab machine's local logs, named per item.  ``calib_2025_1/`` is ``~/ali/data/calib_2025_1/`` since
2026-11-08, when its build folders were deleted: the file times of ``vecunit_res``, ``vu_cg_m`` and
``vu_mf_m`` are kept in its ``logs/file_times/``, and ``vecunit_res``'s ``.err`` / ``.json`` files.
Python rather than JSON: the repository ignores ``*.json``.
"""

from __future__ import annotations

#: ``(unit, what, seconds, source)``: charged to the Waveflow arm.
CHARGED: list[tuple[str, str, float, str]] = [
    ("vector unit", "resources: 10 fit + 3 held-out csynth builds (Stage 3)", 4348.0,
     "calib_2025_1/vecunit_res: each build's .err (start) to .json (result)"),
    ("vector unit", "timing: the CG and MF catalogues, calib + holdout2 + holdout3 + steady runs (Stage 3)",
     7694.0, "calib_2025_1/vu_cg_m 00:34:31-01:48:07, vu_mf_m 00:34:31-01:29:10 (two processes)"),
    ("vector unit", "timing: the catalogues rerun with --bound-factor 2.5 (Stage 3)", 1191.0,
     "calib_2025_1/logs/vu_rerun.log 09:11:19-09:31:10"),
    ("systolic engine", "resources: the 23-point sweep (Stage 4)", 1219.0,
     "calib_2025_1/logs/laneA.log sweep 01:50:44-02:11:03"),
    ("systolic engine", "timing: the eight LTBF points and the two 4 x 8 words (Stage 4)", 709.0,
     "calib_2025_1/logs/laneA.log eh_* 02:11:03-02:22:52"),
    ("A53", "the platform's gem5 campaign, 305 points (Stage 4b)", 2689.0,
     "calib_2025_1/logs/laneB.log cpu_campaign 01:29:43-02:14:32"),
    ("A53", "the platform's cross-configuration check at 2025.1 (Stage 5)", 251.0,
     "calib_2025_1/logs/cpu_cross.log (real 4m11s)"),
    ("A53", "the LTBF kernels ltbf_eig, ltbf_proj, sched_release: smoke + campaign (Stage 4b)", 1060.0,
     "calib_2025_1/logs/laneB.log 02:14:32-02:32:12"),
    ("Shared Mem", "timing and turnaround: the systolic and vector devices traced at both leaves (step 1f)",
     688.0, "dse_cg/logs/devcal.log (105 + 244 + 95 + 244 s)"),
    ("integration", "the six points: 3 + 5 csynth kernels, 4 + 7 part syntheses, 6 RTL runs (step 2c)",
     8891.4, "hw/build/dse/points/*.json: each identity's row once (hls 698 s, synth 2,458 s, rtlsim 5,735 s)"),
    # Charged by the user's decision (2026-11-12): the retired units' calibration, from the same campaign;
    # this system uses SystolicEngine and VecUnit, so no model here reads these.
    ("systolic unit", "linalg_cal fit / holdout / fit2 / holdout3 (Stage 4)", 4459.0,
     "calib_2025_1/logs/laneA.log 02:22:52-03:37:11"),
    ("CG unit", "cg_cal fit / holdout2, and their rerun after the LOOP_FLATTEN fix (Stage 4)", 4832.0,
     "calib_2025_1/logs/laneB.log 02:32:12-03:10:31, laneC.log 09:27:16-10:09:29"),
]

#: ``(unit, what, seconds or None if not totalled, why not charged)``.
NOT_CHARGED: list[tuple[str, str, float | None, str]] = [
    ("system", "the step 1a / 1f gates and the live gate", None,
     "development: they built and checked the framework (hours, several timed out), not a model's input"),
]

#: The Waveflow arm's calibration charge, in tool-seconds (about 10.6 hours).
CALIBRATION_S: float = round(sum(s for _, _, s, _ in CHARGED), 1)
