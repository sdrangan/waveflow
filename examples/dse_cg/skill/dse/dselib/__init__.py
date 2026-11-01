"""dselib -- the library the dse skill explores a design space with.

Every function is a call to the DSE service, which runs the tools and keeps the ledger: what was
evaluated, what it cost, and what budget is left.  The service serves one design (its ``DSE.md``
describes it), so nothing here is specific to a design.  Editing this file changes nothing the
service charges.

Conventions
-----------
* ``params`` is a dict of **design** parameters, ``{name: value}``.  Missing names take their default
  (see :func:`space`).
* ``workload`` is a workload's name from :func:`space`, or a dict ``{"name": ..., <workload
  parameter>: value}`` that overrides that workload's parameters (for example ``{"name": "steady",
  "nit": 4}``).
* Every result is a plain dict, or a list of dicts, ready for ``pandas.DataFrame``.  Every row has
  a ``status`` (``"ok"`` or the reason it failed), and the evaluation rows carry ``cost_s``, the
  tool seconds charged, and ``cached``.  A failure is a row, never an exception.  The exceptions
  raised are for calls that cannot be made at all: an unknown parameter name, an exhausted budget.
* **Exact** means bit-exact: the same number the hardware computes.  **Estimated** means the HLS
  compiler's own report (:func:`hls_estimate`), uncalibrated.  **Approximate** means
  predicted by a calibrated model, and every approximate number carries a ``level``: ``"exact"``
  (the model reproduces every calibration point), ``"interpolated"`` (inside the region it was
  calibrated over), ``"extrapolated"`` (outside it), or ``"uncalibrated"`` (no model behind it).
  **Measured** means from synthesis or RTL simulation.
* Evaluation rows may carry ``artifacts``: ``{name: path}`` of files the service wrote under
  ``results/<eval id>/`` in the working directory -- small, derived files (a utilization report, a
  per-block activity table), never raw waveforms.

The example results below are the CG detector's, and illustrative: their shape is exact, their
numbers are not.  Another design has other parameters, metrics and blocks in the same shape.
"""

from __future__ import annotations

import os
from typing import Any

__all__ = ["space", "golden", "hls_estimate", "synth", "rtlsim", "wait", "results", "budget",
           "submit"]
# arm:waveflow
__all__ += ["pysim"]
# /arm

#: Where the service listens; set by whoever started it (the harness for a run, the user otherwise).
_SERVICE = os.environ.get("DSE_SERVICE", "")

#: A session bound in process (:func:`bind`); when set, calls go to it instead of ``_SERVICE``.
_SESSION: Any = None


def bind(session: Any) -> None:
    """Send every call to ``session``, a ``service.Service`` in this process, instead of one at
    ``DSE_SERVICE``.  For a user whose tools run on this machine; never available in a run's
    sandbox, which has no Waveflow to build a session with."""
    global _SESSION
    _SESSION = session


def _call(fn: str, **kwargs: Any) -> Any:
    """One request: to the bound session if there is one, else to the service at ``DSE_SERVICE``.
    Skeleton: neither path is written yet (plan phase P1b)."""
    raise NotImplementedError(f"dselib.{fn}: the service transport is not built yet")


def space() -> dict:
    """The design space: parameters, constraints and workloads.  Free.

    Returns::

        {"parameters": [
            {"name": "R", "kind": "design", "values": [1, 2, 4, 8, 16], "default": 4,
             "rebuilds": ["systolic"], "description": "systolic array rows"},
            {"name": "nit", "kind": "workload", "values": [1, 2, 3, 4, 5, 6, 7, 8], "default": 4,
             "description": "CG iterations, a field of each command"},
            ...],
         "constraints": [{"rule": "K % R == 0", "why": "the array's rows tile K"}, ...],
         "workloads": [
            {"name": "steady", "fixes": {"nit": 3, "jobs": 4}, "valid_for": ["cycles_per_solve"]},
            {"name": "snr_sweep", "fixes": {"snr_db": [0, 5, 10, 15, 20]},
             "valid_for": ["mse_db", "ber"]},
            ...]}
    """
    return _call("space")


def golden(params: dict, workload: str | dict) -> dict:
    """Untimed, bit-exact simulation: the accuracy the hardware will have.  Cheap.

    Exact for accuracy.  Says nothing about cycles or resources.

    Returns::

        {"params": {...}, "workload": "snr_sweep", "status": "ok",
         "mse_db": {"0": -8.1, "5": -13.0, "10": -21.4, "15": -27.9, "20": -31.2},
         "ber":    {"0": 0.081, "5": 0.012, "10": 4.1e-4, "15": 2.0e-6, "20": 0.0},
         "cost_s": 0.5, "cached": false}
    """
    return _call("golden", params=params, workload=workload)


# arm:waveflow
def pysim(params: dict, workload: str | dict) -> dict:
    """Bit-exact simulation with calibrated, loosely-timed cycles and predicted resources.  Cheap.

    The accuracy is exact, the same as :func:`golden`.  Cycles, per-block activity and resources are
    **approximate**: predicted by models calibrated on synthesis and RTL runs of the units, each with
    a ``level`` saying how far to trust it.  Nothing here is measured; :func:`synth` and
    :func:`rtlsim` measure.

    Returns::

        {"params": {...}, "workload": "steady", "status": "ok",
         "mse_db": {...}, "ber": {...},                                   # exact
         "cycles_per_solve": {"est": 3912, "level": "interpolated"},      # approximate
         "per_block": {                                                   # approximate
            "mm":  {"busy": 0.71, "stalled": {"input": 0.18, "output": 0.04}, "level": "interpolated"},
            "vec": {"busy": 0.52, "stalled": {"input": 0.41, "output": 0.00}, "level": "interpolated"},
            ...},
         "resources": {                                                   # approximate
            "lut":  {"est": 18900, "level": "interpolated"},
            "ff":   {"est": 24100, "level": "interpolated"},
            "dsp":  {"est": 64,    "level": "exact"},
            "bram": {"est": 12,    "level": "exact"}},
         "artifacts": {"activity": "results/e0042/activity.csv"},         # approximate
         "cost_s": 1.1, "cached": false}

    ``busy`` and ``stalled`` are fractions of the run's cycles.  ``stalled["input"]`` is time spent
    waiting for data to arrive, ``stalled["output"]`` waiting for room to send it.
    """
    return _call("pysim", params=params, workload=workload)


# /arm
def hls_estimate(points: list[dict]) -> str:
    """The HLS compiler's estimates for each kernel of the designs.  Moderate; runs as a job.

    Runs Vitis HLS's C synthesis on each HLS kernel and returns its report: estimated resources
    and, where the compiler can compute one, latency.  **Estimated** means the compiler's own
    numbers, before Vivado synthesis: they are known to differ from synthesis, LUTs especially.
    They cover the HLS kernels only.  The adaptors, memory windows and crossbar around the kernels
    are not HLS and have no estimate; :func:`synth` is the first step that sees the whole design.
    A kernel already compiled at the same sizes is not compiled again.

    Returns a job id; :func:`wait` returns one row per design point::

        {"params": {...}, "status": "ok",
         "kernels": {
            "systolic_core": {"lut": 13900, "ff": 16200, "dsp": 48, "bram": 4,
                              "latency": None, "ii": 1, "cached": false},   # None: not computable
            "vec_exec":      {"lut":  6100, "ff":  7400, "dsp": 16, "bram": 8,
                              "latency": 412, "ii": 1, "cached": true},
            ...},
         "artifacts": {"csynth": "results/e0045/csynth_reports/"},
         "cost_s": 70.0, "cached": false}           # cost_s: the kernels compiled this time
    """
    return _call("hls_estimate", points=points)


def synth(points: list[dict]) -> str:
    """Synthesize designs and measure their resources.  Expensive; runs as a job.

    ``points`` is a list of ``params`` dicts.  Returns a job id at once; :func:`wait` returns the
    rows.  A design is synthesized part by part (the parts are listed in ``DSE.md``), and a part
    already synthesized at the same sizes is not built again: only the new parts are charged.

    Each row, the design's totals and each part's own numbers::

        {"params": {...}, "status": "ok",           # or "fails_timing", "over_capacity", ...
         "lut": 18432, "ff": 23870, "dsp": 64, "bram": 12, "ii": 1,
         "parts": {
            "systolic": {"lut": 12100, "ff": 15800, "dsp": 48, "bram": 4, "cached": true},
            "vec":      {"lut":  5200, "ff":  7100, "dsp": 16, "bram": 8, "cached": false},
            "xbar":     {"lut":  1132, "ff":   970, "dsp":  0, "bram": 0, "cached": true}},
         "artifacts": {"utilization": "results/e0043/utilization.txt"},   # per instance
         "cost_s": 61.0, "cached": false}           # cost_s: the parts built this time

    The totals are the sum of the parts, each synthesized on its own.
    """
    return _call("synth", points=points)


def rtlsim(points: list[dict], workload: str | dict) -> str:
    """Simulate designs at RTL, cycle-accurate.  Expensive for a new design; runs as a job.

    A design not yet synthesized is synthesized first, and that is charged too.  Once a design has
    been simulated, a new workload on it costs only the simulation.  Returns a job id; :func:`wait`
    returns the rows.

    Each row::

        {"params": {...}, "workload": "steady", "status": "ok",   # or "deadlock", "mismatch", ...
         "cycles_per_solve": 3870, "bit_exact": true,
         "per_block": {"mm": {"busy": 0.70, "stalled": {"input": 0.19, "output": 0.04}}, ...},
         "artifacts": {"activity": "results/e0044/activity.csv"},
         "cost_s": 31.0, "cached": false}

    ``activity.csv`` is the same table as pysim's, measured: one row per block and interval, with
    the fractions busy, stalled on input and stalled on output.
    """
    return _call("rtlsim", points=points, workload=workload)


def wait(job: str | list[str]) -> list[dict]:
    """Block until the job (or every job in a list) is done, and return its rows.

    Waiting is free: the tool clock advances to the job's completion, not by your thinking time.
    """
    return _call("wait", job=job)


def results(fn: str | list[str] | None = None) -> list[dict]:
    """Every evaluation so far, one row per design point and call, in the order made.  Free.

    ``fn`` keeps only those functions' rows (``"pysim"``, or ``["synth", "rtlsim"]``).  The rows
    are the ones the functions returned, flattened so that ``pandas.DataFrame(results(...))`` needs
    no further work: ``params`` become one column per parameter, and nested values become dotted
    columns (``lut.est``, ``lut.level``, ``parts.vec.lut``, ``per_block.mm.busy``).

        df = pd.DataFrame(results("pysim"))
        measured = pd.DataFrame(results("synth"))
        df.merge(measured, on=[p["name"] for p in space()["parameters"] if p["kind"] == "design"])

    The service keeps this record, so it is complete and exact: use it to recover your state
    instead of repeating calls or relying on notes.
    """
    return _call("results", fn=fn)


def budget() -> dict:
    """The ledger.  Free.

    Returns::

        {"budget_s": 144000, "spent_s": 51230, "remaining_s": 92770,
         "by_function": {"golden": 120.0, "synth": 48100.0, "rtlsim": 3010.0, ...},
         "jobs_running": 2, "jobs_queued": 0, "tool_clock_s": 30215}
    """
    return _call("budget")


def submit(params: dict, report: dict) -> dict:
    """End the run with your answer.

    ``params`` must be a design this run has synthesized **and** RTL-simulated, on the workloads the
    objective names; otherwise the call is refused and the run continues.  ``report`` is described in
    the skill's ``SKILL.md``.  After a successful submit, every other call is refused.
    """
    return _call("submit", params=params, report=report)
