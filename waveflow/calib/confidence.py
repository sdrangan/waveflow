"""confidence.py — what a calibrated model knows about its own prediction.

A predicted number alone is not an answer.  An agent exploring a design space against bare floats will
optimize straight out of the calibrated region and confidently report a design that does not exist, so
every prediction has to carry *how much it should be believed*.

**Not as an interval.**  The obvious move — attach ``lo``/``hi`` — is wrong here in principle, not just
in practice.  Synthesis is deterministic: run csynth twice at one point and the LUT count is identical.
There is no noise process, so the classical prediction interval
(``ŷ ± t·s·√(1 + xᵀ(XᵀX)⁻¹x)``) has nothing to estimate.  The error that actually occurs is *model
misspecification* — the gap between a fitted form and a deterministic, discontinuous truth — and
misspecification is not measurable from inside the model.  Worse, it is systematic exactly where it
matters: near a storage-partitioning threshold a smooth model's residual is not random, it is
consistently wrong in one direction, and a Gaussian interval would understate it.

Nor is it a cross-validated spread.  An affine span law fit at ``n=128`` and ``n=256`` predicts every
other ``n`` *exactly* — that is a claim about the form, not about a sample.  Running LOO-CV over it
would manufacture a ±3% band where the true error is zero, which is not conservative, it is wrong.
Only the model knows its own epistemic situation, so the model is what speaks.

**So: a level plus a free-form fact dict.**

* :class:`ConfidenceLevel` is a small **closed** enum whose only job is to be *sortable*, so a report
  over eleven modules can be triaged without reading eleven paragraphs.  It stays closed on purpose —
  the moment it grows, the ordering becomes ambiguous (is ``SPARSE_INTERPOLATION`` better or worse than
  ``LINEARLY_EXTRAPOLATED``?) and triage, its entire reason for existing, breaks.  The test for adding
  a level is whether a consumer would take a *different action*; the actions available are trust it,
  spend a calibration, or avoid the region.
* :attr:`Confidence.facts` is whatever that model wants to say, in its own vocabulary, JSON-able.  The
  primary consumer is an agent, which handles ``fit_range`` vs ``support`` as synonyms without help —
  so a shared vocabulary would buy nothing and would hit the same proliferation problem one level down.

Two guards keep the freedom from becoming a junk drawer, both cheap:

1. **Serializability is checked at construction**, so a stray ``numpy.float64`` fails next to the model
   that produced it rather than at report-dump time.
2. **An ``EXACT`` claim must be backed** by zero residual on the model's own corpus
   (:meth:`~waveflow.calib.calib.CalibModel.confidence` enforces it).  Otherwise ``EXACT`` becomes the
   unexamined default that nobody validates.

One convention, not enforced: a ``summary`` key holding a one-line human string.  The agent reads the
whole dict, but a person reading a PySim report over eleven modules wants a sentence.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ConfidenceError(ValueError):
    """A :class:`Confidence` was built with a non-serializable fact, or an unbacked ``EXACT`` claim."""


class ConfidenceLevel(str, Enum):
    """How much a prediction should be believed.  Ordered; see :attr:`rank`.

    * ``EXACT`` — the model's form reproduces every calibration point with zero residual, so the
      prediction is not an approximation.  A claim, and a checked one.
    * ``INTERPOLATED`` — the query lies inside the region the model was fit over.
    * ``EXTRAPOLATED`` — outside it.  For resources this is the level that matters most, because what
      you cross on the way out is usually a *regime boundary* (a DSP-vs-LUT inference threshold, a
      BRAM-vs-LUTRAM partitioning threshold), and those move several counters at once.
    * ``UNCALIBRATED`` — no fit backs this number; it came from a seed or a default.
    """

    EXACT = "EXACT"
    INTERPOLATED = "INTERPOLATED"
    EXTRAPOLATED = "EXTRAPOLATED"
    UNCALIBRATED = "UNCALIBRATED"

    @property
    def rank(self) -> int:
        """Higher is stronger.  Sorting a report by this surfaces the weakest link first."""
        return {"UNCALIBRATED": 0, "EXTRAPOLATED": 1, "INTERPOLATED": 2, "EXACT": 3}[self.value]

    def __lt__(self, other: object) -> bool:      # so min()/sorted() work directly on levels
        if isinstance(other, ConfidenceLevel):
            return self.rank < other.rank
        return NotImplemented


def _assert_jsonable(facts: dict) -> None:
    """Fail at the model that produced a non-serializable fact, not at report-dump time."""
    try:
        json.dumps(facts)
    except (TypeError, ValueError) as exc:
        raise ConfidenceError(
            f"confidence facts are not JSON-serializable ({exc}).  The facts dict is dumped verbatim "
            f"for an agent to consume, so every value must survive json.dumps — convert numpy scalars "
            f"with float()/int() and Paths with str() at the model."
        ) from None


@dataclass(frozen=True)
class Confidence:
    """A level plus whatever the model wants to say about why.

    ``facts`` is free-form and model-specific; only ``level`` is guaranteed, and it is what
    :meth:`to_json` always emits alongside the rest.
    """

    level: ConfidenceLevel
    facts: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.level, ConfidenceLevel):
            object.__setattr__(self, "level", ConfidenceLevel(str(self.level)))
        _assert_jsonable(self.facts)

    @property
    def summary(self) -> str:
        """The conventional one-line human string, or a fallback built from the level."""
        return str(self.facts.get("summary") or f"{self.level.value.lower()} (no summary given)")

    def to_json(self) -> dict:
        """One flat dict with ``level`` guaranteed present — what gets handed to an agent."""
        return {"level": self.level.value, **self.facts}

    @classmethod
    def uncalibrated(cls, why: str) -> "Confidence":
        """The no-fit-backs-this case, which is otherwise easy to leave silent.

        ``CalibModel.load_or_default`` falls back to seed parameters today with no signal at all; this
        is what makes that visible in a report.
        """
        return cls(level=ConfidenceLevel.UNCALIBRATED, facts={"summary": why})


@dataclass(frozen=True)
class Estimate:
    """A predicted value together with where it came from and how much to believe it.

    Deliberately *not* what :meth:`~waveflow.calib.calib.CalibModel.predict` returns.  ``predict`` is
    on the simulation hot path — called per firing, feeding straight into arithmetic — so it stays a
    bare float.  An :class:`Estimate` is built once per module at *report* time, which is the right
    frequency for the work of assembling confidence.
    """

    value: float
    source: str = ""
    confidence: Confidence = field(
        default_factory=lambda: Confidence.uncalibrated("no model consulted"))

    @property
    def level(self) -> ConfidenceLevel:
        return self.confidence.level

    def to_json(self) -> dict:
        return {"value": self.value, "source": self.source,
                "confidence": self.confidence.to_json()}


@dataclass(frozen=True)
class FitSummary:
    """The little that must be retained from a fit for a *deployed* model to still report confidence.

    A fitted model keeps its coefficients and throws the training data away, which is what lets a
    published artifact predict with no sklearn and no corpus.  But confidence needs to know the region
    that was covered and how well the form matched — so this is the minimum carried alongside the
    coefficients: per-feature support, the point count, and the worst residual.

    It is small enough to live in the model artifact, which matters: a platform library reused by a
    project that never ran the sweep can still say *"you are extrapolating."*
    """

    features: list[str] = field(default_factory=list)
    ranges: dict = field(default_factory=dict)      # feature -> [lo, hi] seen during the fit
    n_points: int = 0
    max_abs_residual: float = 0.0
    max_rel_residual: float = 0.0
    n_free_params: int = 0

    #: Residuals below this are treated as zero, so an exactness claim survives float round-off.
    EXACT_TOL: float = 1e-9

    @property
    def is_exact(self) -> bool:
        """True when the fitted form reproduced **more points than it has free parameters**.

        The point-count condition is what keeps this honest, and it is easy to get wrong.  An affine
        law has two free parameters, so fitting it at exactly two points gives zero residual *by
        construction* — the line passes through both because it must, which is evidence of nothing.
        Only once the fit is over-determined does a zero residual say something about the *form*:
        that the law kept holding at points it was not free to match.
        """
        return (self.n_points > max(self.n_free_params, 0)
                and self.max_abs_residual <= self.EXACT_TOL)

    @property
    def degenerate(self) -> bool:
        """True when the fit had no more points than free parameters — zero residual is meaningless."""
        return 0 < self.n_points <= max(self.n_free_params, 0)

    def covers(self, row: dict) -> bool:
        """True when every known feature of *row* lies inside the fitted range.

        Features absent from *row* or from the fit are not evidence of extrapolation and are skipped —
        a missing feature is unknown, not out of range.
        """
        for name, bounds in self.ranges.items():
            if name not in row:
                continue
            try:
                value = float(row[name])
            except (TypeError, ValueError):
                continue
            lo, hi = float(bounds[0]), float(bounds[1])
            if not (lo <= value <= hi or math.isclose(value, lo) or math.isclose(value, hi)):
                return False
        return True

    def outside(self, row: dict) -> dict:
        """``{feature: [value, lo, hi]}`` for each feature of *row* that falls outside the fit.

        The fact an agent acts on: *which* knob left the calibrated region, and by how much.
        """
        out: dict = {}
        for name, bounds in self.ranges.items():
            if name not in row:
                continue
            try:
                value = float(row[name])
            except (TypeError, ValueError):
                continue
            lo, hi = float(bounds[0]), float(bounds[1])
            if not (lo <= value <= hi or math.isclose(value, lo) or math.isclose(value, hi)):
                out[name] = [value, lo, hi]
        return out

    def to_json(self) -> dict:
        return {"features": list(self.features), "ranges": dict(self.ranges),
                "n_points": int(self.n_points),
                "max_abs_residual": float(self.max_abs_residual),
                "max_rel_residual": float(self.max_rel_residual),
                "n_free_params": int(self.n_free_params)}

    @classmethod
    def from_json(cls, data: Any) -> "FitSummary | None":
        if not isinstance(data, dict):
            return None
        return cls(features=list(data.get("features") or []),
                   ranges=dict(data.get("ranges") or {}),
                   n_points=int(data.get("n_points", 0) or 0),
                   max_abs_residual=float(data.get("max_abs_residual", 0.0) or 0.0),
                   max_rel_residual=float(data.get("max_rel_residual", 0.0) or 0.0),
                   n_free_params=int(data.get("n_free_params", 0) or 0))
