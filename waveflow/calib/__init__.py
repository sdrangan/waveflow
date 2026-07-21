"""waveflow.calib — bare-bones calibration model-fitting infrastructure.

A small, reusable corpus + model layer for fitting **physically-reasonable
primitive timing models** from synth/cosim datapoints (the
``project-cycle-model-training`` vision: keep timing artifacts structured and the
parameters programmatic).  It is deliberately minimal — a ``pandas.DataFrame``
wrapper plus sklearn / interp models with held-out / R² / plot helpers — *not* an
ML framework.

* :class:`CalibDataFrame` — one row per synth/cosim datapoint, backed by a
  ``pandas.DataFrame`` (``.df``); filter/select with native pandas.
* :class:`CalibModel` — base fit/predict/score interface (per-target).  Basis /
  target are column-name strings; any transform is a caller-side derived column.
* :class:`LinCalibModel` — sklearn ``LinearRegression``-backed.  FIR uses the
  pure-linear basis ``[num_trans, nwords]``.
* :class:`InterpCalibModel` — a calibrated 1-D saturating lookup (FIR's ``row_depth(n_col)``).
"""
from .calib import (
    CalibDataFrame,
    CalibModel,
    InterpCalibModel,
    LinCalibModel,
)
from .timing_model import StreamTimingModel, TimingModel

__all__ = [
    "CalibDataFrame",
    "CalibModel",
    "InterpCalibModel",
    "LinCalibModel",
    "StreamTimingModel",
    "TimingModel",
]
