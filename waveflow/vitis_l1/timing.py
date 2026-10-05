"""timing.py — ``VitisFft``'s two timing models: the processing delay and the frame interval.

``VitisFft`` (:mod:`waveflow.vitis_l1.hw`) has two separately measurable timing events, so two
:class:`~waveflow.calib.timing_model.TimingModel` s, one target each:

* **proc** -- the delay ``run_iter`` hands :meth:`~waveflow.simulation.simobj.SimObj.call_after`:
  from a frame's last input word in the block to its last output word out.  Measured on **isolated**
  frames, which find the block idle.
* **ii** -- the frame interval: how long after one frame's intake the block takes the next.
  Measured **back to back**, where the block is saturated.

Both are stored on the platform, so they are properties of ``(component, part, clock)`` and reload
in any design on that platform.

**A lookup per L, not a law in L.**  The first candidate was a law,
``clk.period * (b0 + b1 L + b2 L log2 L)`` -- a fixed overhead, a per-sample cost, a per-stage cost.
It fits L = 16, 64, 256 exactly and fails across the next one: fit on those three, it predicts
L = 1024 14% low (proc) and 20% low (ii).  Between 256 and 1024 Vitis changes the implementation (the
twiddles move into a ROM, a fifth stage appears), and per-sample cost steps from 7.5 to 10 cycles per
``L/R``.  That is the case :class:`~waveflow.calib.calib.LookupCalibModel` exists for: exact where
measured, and an unmeasured ``L`` is ``UNCALIBRATED`` rather than an extrapolation that would be
confidently wrong.  ``law="linear"`` keeps the other option, and the fixture's ``--holdout`` keeps
the evidence reproducible.  The residual framing (``rtl_span - pysim_span + current_dly``) means ``run_iter`` only adds
predicted delays: whatever the channels already charge (the frame's transfers) is subtracted out by
the fit, so the module never restates a channel's timing.

**The proc model aggregates by MEAN, not median.**  An isolated frame's latency depends on its arrival
phase against the vendor core's free-running input commutator, which an LT model cannot know.  The
pysim therefore outputs one number per configuration, and the number that minimizes the squared error
over arrival phases is the mean.  The spread across phases is irreducible and is reported, not
modelled (see :func:`proc_spread`).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from waveflow.calib.timing_model import TimingModel

#: The platform ``VitisFft`` is calibrated on: the RFSoC 4x2 at 250 MHz, measured through the XSI BFM.
PLATFORM = "rfsoc4x2_bfm_250mhz"

#: The regression basis.  The intercept is implicit (``fit_intercept``), so the model is
#: ``b0 + b1 L + b2 L log2 L`` in cycles.
FEATURES = ["L", "L_log2L"]


def features(length: int) -> dict[str, float]:
    """The basis values for an ``L``-point transform."""
    return {"L": float(length), "L_log2L": float(length * math.log2(length))}


def component_ids(task_fn: str, in_w: int, in_i: int, tw_w: int, tw_i: int, scaling: int,
                  order: int) -> tuple[str, str]:
    """``(proc, ii)`` component keys on the platform.

    Qualified by every template argument **except** ``L`` (and ``OUT_W``, which ``L`` determines): the
    model is a function of ``L``, so one directory serves every length -- that is what lets it be fit
    across lengths at all -- while a different word or twiddle width is a different datapath and gets
    its own.
    """
    cfg = f"{task_fn}_{in_w}_{in_i}_{tw_w}_{tw_i}_{scaling}_{order}"
    return f"{cfg}.proc", f"{cfg}.ii"


@dataclass
class VitisFftTimingModel(TimingModel):
    """A :class:`TimingModel` on the ``[L, L log2 L]`` basis whose per-run aggregate is selectable.

    The base reduces each run's firings to their **median** span, which is right for a component
    whose firings are one steady-state cost with outliers.  The proc model's firings are a spread of
    arrival phases, and the pysim uses one number for all of them, so the right aggregate there is
    the **mean**: it minimizes the squared error the pysim will make.
    """

    aggregate: str = "median"
    #: ``"lookup"`` (exact per measured L; see the module docstring) or ``"linear"`` (the
    #: ``[1, L, L log2 L]`` law the hold-out rejected).
    law: str = "lookup"

    def __post_init__(self) -> None:
        if not self.features:
            self.features = list(FEATURES)
        super().__post_init__()
        if self.law == "lookup":
            from waveflow.calib.calib import LookupCalibModel
            from waveflow.calib.timing_model import RESIDUAL
            # Seeded with an EMPTY table: a configuration never calibrated predicts no extra delay
            # and reports UNCALIBRATED -- what the fixture's first, zero-seed pysim pass expects (and
            # what VitisFft refuses when require_calibrated).  Without a seed a new configuration
            # could not even be calibrated.
            self._model = LookupCalibModel(basis=["L"], target=RESIDUAL, name=self.component,
                                           path=self.calib_dir / "params.json",
                                           seed={"basis": ["L"], "targets": [RESIDUAL], "table": []})
        elif self.law != "linear":
            raise ValueError(f"law must be 'lookup' or 'linear', got {self.law!r}")

    def get_params(self, run_dir: Path, validate: bool = True) -> dict | None:
        if self.aggregate == "median":
            return super().get_params(run_dir, validate)
        p = Path(run_dir) / "firings.csv"
        if not p.exists() or p.stat().st_size == 0:
            return None
        df = pd.read_csv(p)
        if validate and not df.empty:
            df = df[[self.is_record_valid(r, Path(run_dir)) for r in df.to_dict("records")]]
        if df.empty:
            return None
        row = {f: float(df[f].iloc[0]) for f in self.features}
        for c in df.columns:
            if c not in self.features and pd.api.types.is_numeric_dtype(df[c]):
                row[c] = float(getattr(df[c], self.aggregate)())
        return row


def proc_spread(proc_dir: Path, length: int) -> tuple[float, float] | None:
    """``(min, max)`` of the measured isolated-frame proc spans at *length*, in cycles, relative to
    their mean: the error the mean model makes on individual frames.  ``None`` if not measured."""
    for run in sorted((Path(proc_dir) / "rtl").glob("*")):
        f = run / "firings.csv"
        if not f.exists():
            continue
        df = pd.read_csv(f)
        if df.empty or float(df["L"].iloc[0]) != float(length):
            continue
        s = df["span"].astype(float)
        return float(s.min() - s.mean()), float(s.max() - s.mean())
    return None
