"""measure.py — one RTL run of a vector unit's harness, recorded for a catalogue
(``plans/vecunit_new.md`` Stage 9c), as ``examples/systolic/systolic_measure.py`` records the systolic
engine's: whether every output is exact, each job's status cycle, pysim's, csynth's resources and
clock, the execute task's loop schedule, each job's bounds (:func:`waveflow.vecunit.report.job_bounds`)
and, on a traced run, which task binds (:func:`waveflow.vecunit.report.trace_report`).

Application-free: the examples' scripts (``examples/vecunit_cg/cg_measure.py``,
``examples/vecunit_mf/mf_measure.py``) name the builds and workloads and keep ``measured.json``; the
calibration (:mod:`waveflow.vecunit.calib`, Stage 9d) reads them.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

from waveflow.vecunit.build import PERIOD_NS


def measure_run(unit, workload: str, root: Path, *, trace: bool = False) -> dict:
    """*unit*'s harness on *workload* in pysim, then through csynth and XSI at *root* (one root per
    build: csynth runs on its first workload only), its RTL bounded a little past pysim's length."""
    from waveflow.hw.harness import harness
    from waveflow.vecunit import report
    from waveflow.vecunit.calib import pysim_status
    from waveflow.vecunit.hls import header_name
    from waveflow.vecunit.runtime import PYSIM_BOUND, rtl_bound, time_rtl

    sysm = harness(unit, workload=workload, n_cycles=PYSIM_BOUND)
    sysm.run()
    pysim_cycles = sysm.sim.env.now / sysm.clock().period
    rtl = time_rtl(unit, workload, Path(root), rtl_bound(pysim_cycles), trace=trace)
    out = {"header": header_name(unit.cfg), "word_bits": int(unit.cfg.word_bits),
           "workload": workload, "tool": rtl["tool"], "exact": rtl["exact"],
           "failed": rtl["failed"]}
    if not rtl["exact"]:
        return out
    status = rtl["status_cycles"]
    res = report.resources(Path(root))["unit"]
    out.update({
        "status_cycles": status, "intervals": np.diff(status).astype(int).tolist(),
        "done_cycle": rtl["done_cycle"], "pysim_cycles": pysim_cycles,
        "pysim_status": [round(x, 1) for x in pysim_status(unit, workload)],
        "resources": res, "meets_clock": res["clock_ns"] <= PERIOD_NS,
        "schedule": report.loop_schedule(Path(root)),
        "bounds": [{k: (round(v, 1) if isinstance(v, float) else v)
                    for k, v in report.job_bounds(unit.cfg, p).items()}
                   for _tx, p, _inputs in unit.jobs(workload)],
        "simulate_s": rtl["simulate_s"]})
    if trace:
        out["trace"] = report.trace_report(report.load_unit_trace(unit, Path(root)), Path(root))
    return out


__all__ = ["measure_run"]
