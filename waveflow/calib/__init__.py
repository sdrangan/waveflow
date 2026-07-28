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
from .confidence import (
    Confidence,
    ConfidenceError,
    ConfidenceLevel,
    Estimate,
    FitSummary,
)
from .fixture import ComponentFixture, SweepPoint
from .module_key import (
    ModuleIdentity,
    UnboundModuleError,
    UnstableSignatureError,
    identify,
    identify_instance,
    module_key,
    walk_modules,
)
from .record_store import (
    KeyCollisionError,
    ModuleStore,
    Provenance,
    Record,
    StaleRecordError,
    resource_record,
)
from .timing_model import StreamTimingModel, TimingModel

__all__ = [
    "CalibDataFrame",
    "CalibModel",
    "ComponentFixture",
    "Confidence",
    "ConfidenceError",
    "ConfidenceLevel",
    "Estimate",
    "FitSummary",
    "InterpCalibModel",
    "KeyCollisionError",
    "LinCalibModel",
    "ModuleIdentity",
    "ModuleStore",
    "Provenance",
    "Record",
    "StaleRecordError",
    "StreamTimingModel",
    "SweepPoint",
    "TimingModel",
    "UnboundModuleError",
    "UnstableSignatureError",
    "identify",
    "identify_instance",
    "module_key",
    "resource_record",
    "walk_modules",
]
