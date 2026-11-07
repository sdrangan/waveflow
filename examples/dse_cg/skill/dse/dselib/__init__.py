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
  parameter>: value}`` that overrides that workload's parameters (for example ``{"name": "long_term",
  "t": 2}``).
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

The example results below are the LTBF system's, and illustrative: their shape is exact, their
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


class Refused(Exception):
    """The service refused the call: a wrong arm, an exhausted budget, a closed run, a bad name."""


def _call(fn: str, **kwargs: Any) -> Any:
    """One request: to the bound session if there is one, else to the service at ``DSE_SERVICE``
    (``host:port``; an HTTP ``POST /call``).  A refusal raises :class:`Refused`."""
    if _SESSION is not None:
        try:
            return getattr(_SESSION, fn)(**kwargs)
        except Exception as e:  # the service's own refusal type lives with the service
            if type(e).__name__ == "Refused":
                raise Refused(str(e)) from None
            raise
    if not _SERVICE:
        raise Refused("no DSE service: set DSE_SERVICE to its host:port")
    import json
    import urllib.request

    req = urllib.request.Request(f"http://{_SERVICE}/call", method="POST",
                                 data=json.dumps({"fn": fn, "kwargs": kwargs}).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=None) as r:
        out = json.loads(r.read())
    if "refused" in out:
        raise Refused(out["refused"])
    return out["ok"]


def space() -> dict:
    """The design space: parameters, constraints and workloads.  Free.

    Returns::

        {"parameters": [
            {"name": "R", "kind": "design", "values": [2, 4, 8], "default": 4,
             "rebuilds": ["sys"], "description": "systolic array rows"},
            {"name": "mem_rate", "kind": "design", "values": [2, 1], "default": 2,
             "rebuilds": [], "description": "Shared Mem's cycles per read word"},   # simulation only
            {"name": "t", "kind": "workload", "values": [2, 3], "default": 3,
             "description": "CG iterations of the long-term update"},
            ...],
         "constraints": [{"rule": "(R, C) in [(2, 4), (4, 4), (4, 8), (8, 8)]",
                          "why": "the systolic shapes built and calibrated"}, ...],
         "workloads": [
            {"name": "long_term", "fixes": {"drop": 1, "sector": 1}, "valid_for": ["t_upd_ms"]},
            {"name": "accuracy", "fixes": {"drops": "11-100"},
             "valid_for": ["median_loss_db", "p90_loss_db"]},
            ...]}

    A design parameter whose ``rebuilds`` is empty changes no part: a new value costs an RTL
    simulation and no synthesis.
    """
    return _call("space")


def golden(params: dict, workload: str | dict) -> dict:
    """Untimed, bit-exact simulation: the accuracy the hardware will have.  Cheap.

    Exact for accuracy.  Says nothing about cycles or resources.

    Returns::

        {"params": {...}, "workload": {"name": "accuracy", "target": "-6_3"}, "status": "ok",
         "median_loss_db": 0.086, "p90_loss_db": 0.31,
         "cost_s": 40.0, "cached": false}
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

        {"params": {...}, "workload": "long_term", "status": "ok",
         "median_loss_db": {...},                                         # exact
         "t_upd_ms": {"est": 6.4, "level": "interpolated"},               # approximate
         "per_block": {                                                   # approximate
            "sys": {"busy": 0.83, "stalled": {"input": 0.61, "output": 0.02}, "level": "interpolated"},
            "vec": {"busy": 0.12, "stalled": {"input": 0.08, "output": 0.00}, "level": "interpolated"},
            "mem": {"busy": 0.90, "level": "uncalibrated"},               # a modelled part's port
            ...},
         "resources": {                                                   # approximate
            "lut":  {"est": 100700, "level": "interpolated"},
            "ff":   {"est": 105100, "level": "interpolated"},
            "dsp":  {"est": 253,    "level": "exact"},
            "bram": {"est": 546,    "level": "interpolated"}},
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
            "ltbf_sys": {"lut": 38700, "ff": 48200, "dsp": 144, "bram": 24,
                         "latency": None, "ii": 1, "cached": false},   # None: not computable
            "ltbf_hub": {"lut":   600, "ff":   900, "dsp":   0, "bram":  0,
                         "latency": None, "ii": 1, "cached": true},
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
         "lut": 55400, "ff": 54900, "dsp": 105, "bram": 518, "ii": 1,
         "parts": {
            "sys":  {"lut": 10256, "ff": 12841, "dsp": 47, "bram":   4, "cached": true},
            "vec":  {"lut": 41387, "ff": 40037, "dsp": 58, "bram":  58, "cached": false},
            "xbar": {"lut":  3003, "ff":  1169, "dsp":  0, "bram":   0, "cached": true},
            "mem":  {"bram": 456, "source": "formula"},   # modelled, not synthesized
            ...},
         "artifacts": {"utilization": "results/e0043/utilization.txt"},   # per instance
         "cost_s": 61.0, "cached": false}           # cost_s: the parts built this time

    The totals are the sum of the parts, each synthesized on its own.
    """
    return _call("synth", points=points)


def rtlsim(points: list[dict], workload: str | dict) -> str:
    """Simulate designs at RTL, cycle-accurate.  Expensive for a new design; runs as a job.

    A design not yet synthesized is synthesized first, and that is charged too.  Once a design's
    RTL is elaborated, a new workload on it, or a new value of a parameter that rebuilds no part,
    costs only the simulation.  Returns a job id; :func:`wait` returns the rows.

    Each row::

        {"params": {...}, "workload": "long_term", "status": "ok",   # or "deadlock", "mismatch", ...
         "t_upd_ms": 6.6, "bit_exact": true,
         "per_block": {"sys": {"busy": 0.82, "stalled": {"input": 0.62, "output": 0.02}}, ...},
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
    columns (``lut.est``, ``lut.level``, ``parts.vec.lut``, ``per_block.sys.busy``).

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
