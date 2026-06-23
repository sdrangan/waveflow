"""waveflow.calib — bare-bones calibration model-fitting infrastructure.

A small, reusable corpus + model layer for fitting **physically-reasonable
primitive timing models** from synth/cosim datapoints (the
``project-cycle-model-training`` vision: keep timing artifacts structured and the
parameters programmatic).  It is deliberately minimal — an sklearn wrapper plus a
datapoint database with held-out / R² / plot helpers — *not* an ML framework.

* :class:`CalibDatabase` — one row per synth/cosim datapoint (the structured corpus).
* :class:`CalibModel` — base fit/predict/score interface (per-target).
* :class:`LinCalibModel` — sklearn ``LinearRegression``-backed, with feature
  transforms / basis functions (e.g. ``sqrt``, products) for generality.  FIR uses
  the pure-linear basis ``[num_trans, nwords]``; the transforms exist so the class
  generalizes, not to reintroduce a fudge term.
"""
from .calib import (
    CalibDatabase,
    CalibModel,
    Feature,
    InterpCalibModel,
    LinCalibModel,
)

__all__ = [
    "CalibDatabase",
    "CalibModel",
    "Feature",
    "InterpCalibModel",
    "LinCalibModel",
]
