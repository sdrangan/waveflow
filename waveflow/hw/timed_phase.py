"""timed_phase.py — timing a module's body between events at its ports (``plans/pipelined_ops.md``).

A module **declares** its timed phases once, beside its endpoints; its body reads and writes as it
always has and, between them, lets the phase wait its predicted duration::

    # __post_init__
    self.compute_phase = TimedPhase(
        "compute", spans={"compute_time": (self.s_in.last, self.m_out.first)}, features=("n",))
    self.add_timing_phase(self.compute_phase)

    # the body, per message (Pattern 1: sequential load, compute, store)
    x = yield from self.s_in.get_array(Float32, n)        # returns at s_in.last
    y = compute(x)
    yield self.compute_phase.timeout(n=n)                 # waits compute_time; opens the record
    yield from self.m_out.write(y)                        # its first beat closes the record

The two rules (the plan's): a **span** is the time between two endpoint events -- a transfer's first or
last beat (:class:`TimingEvent`, ``ep.first`` / ``ep.last``) -- whose duration is a deterministic
function of **features** known before the first event; its **model** learns that function.  Every event
is mechanically measurable at RTL (the endpoint's handshake) and logged by pysim's own transfer
primitive (``Xfer`` records on each endpoint that carries a declared event).  A phase and its model are
one object: the spans are the model's named targets.  Durations are in **cycles** of the owner's clock,
so a fit is clock-independent.

The pieces: :class:`TimingEvent`, :class:`TimedPhase`, :class:`LinearTiming` (the default model
family), :class:`PhaseRecord` (one occurrence, pysim's half of the calibration corpus), and
:func:`note_transfer` -- the hook the endpoints call when a logged transfer ends.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

EDGES = ("first", "last")


@dataclass(frozen=True, eq=False)
class TimingEvent:
    """A marker on one endpoint: the ``first`` or ``last`` beat of each of its transfers.

    Names an **operation on an endpoint**, never a role ("load", "store") and never a message: which
    transfer an occurrence refers to is bound at run time.  Build it from the endpoint --
    ``self.s_in.first`` -- rather than directly."""

    ep: Any
    edge: str
    #: An ``m_axi`` endpoint's AXI channel ("R" / "W"); a stream has none.  (``m_axi`` events are
    #: deferred in the plan; the field keeps the door open.)
    channel: str | None = None

    def __post_init__(self) -> None:
        if self.edge not in EDGES:
            raise ValueError(f"a timing event is an endpoint's 'first' or 'last' beat, not {self.edge!r}")

    def __repr__(self) -> str:
        return f"{getattr(self.ep, 'name', '?')}.{self.edge}"


@dataclass
class LinearTiming:
    """The default model family: each span's duration is ``intercept + sum(coef_f * feature_f)``
    cycles -- one :class:`~waveflow.calib.calib.LinCalibModel` per span (``CalibModel``'s rule: one
    model per target), over the phase's features.

    A family states the law's **shape**, never its numbers: the numbers are fitted, or loaded from the
    calibration store, or -- before either -- the phase's ``seed``."""

    def build(self, phase: "TimedPhase") -> dict:
        from waveflow.calib.calib import LinCalibModel

        feats = list(phase.features)
        out = {}
        for span in phase.spans:
            seed = {f: 0.0 for f in feats}
            seed["intercept"] = 0.0
            seed.update((phase.seed or {}).get(span, {}))
            out[span] = LinCalibModel(basis=feats, target=span, name=f"{phase.name}.{span}",
                                      fit_intercept=True, coeff_names=feats, seed=seed)
        return out


@dataclass
class PhaseRecord:
    """One occurrence of a phase, per span: where it started and ended (``(endpoint name, transfer
    index, edge, time)``), the features, the predicted and the actual duration (cycles), and whether
    the environment held it (the actual is later than predicted, or a transfer it rests on stalled).
    pysim's half of the calibration corpus: the RTL half is the same events, measured at the ports,
    joined on ``(endpoint, index)``."""

    phase: str
    span: str
    occurrence: int
    start: tuple
    features: dict
    predicted: float
    end: tuple | None = None
    actual: float | None = None
    stalled: bool = False


class TimedPhase:
    """A declared timed phase: named **spans** (each a pair of :class:`TimingEvent`\\ s), the
    **features** its durations depend on, and its model.

    Registered like an endpoint -- construct, assign, then ``self.add_timing_phase(phase)`` -- which
    binds it to its **owner**, the module: through the owner it reaches the clock and checks that every
    event is on one of the module's own endpoints.

    * :meth:`timeout` -- Pattern 1, a sequential body: wait one span, measured from its start event,
      and arrange for the next transfer on its end endpoint to close the record.
    * :attr:`tm` -- the learned functions, one per span (:class:`LinearTiming` by default), usable with
      no simulator: :meth:`predict` is plain arithmetic.
    * :attr:`records` -- every occurrence (:class:`PhaseRecord`), the pysim half of a calibration.
    """

    #: Context, never structure: a phase predicts something *about* a module (``CalibModel``'s rule),
    #: so a module's content-addressed identity must not move when its phases or their numbers do.
    __wf_structure_context__: ClassVar[bool] = True

    def __init__(self, name: str, spans: dict, features=(), tm=None, seed: dict | None = None,
                 calib_dir: str | Path | None = None):
        if not spans:
            raise ValueError(f"phase {name!r}: a phase has at least one span")
        for span, ends in spans.items():
            if (not isinstance(ends, tuple) or len(ends) != 2
                    or not all(isinstance(e, TimingEvent) for e in ends)):
                raise TypeError(f"phase {name!r}, span {span!r}: a span is (start event, end event), "
                                f"each an endpoint's .first or .last -- got {ends!r}")
        self.name = name
        self.spans: dict[str, tuple[TimingEvent, TimingEvent]] = dict(spans)
        self.features: tuple[str, ...] = tuple(features)
        #: Seed numbers per span, ``{span: {"intercept": c, feature: coef}}``: what the model predicts
        #: before any fit.  A starting point, replaced by fitted parameters when the store has them.
        self.seed = seed
        #: Where fitted parameters live (``<calib_dir>/<span>.json``); ``None``: the seed only.
        self.calib_dir = Path(calib_dir) if calib_dir is not None else None
        self.family = tm if tm is not None else LinearTiming()
        self.owner = None
        self.records: list[PhaseRecord] = []
        self._models: dict | None = None
        self._used: dict[str, int] = {}          # start endpoint name -> last transfer index used
        self._occurrences = 0

    def __repr__(self) -> str:
        return f"TimedPhase({self.name!r}, spans={self.spans!r}, features={self.features!r})"

    # -- the model -------------------------------------------------------------------------------
    @property
    def tm(self) -> dict:
        """``{span: model}`` -- loaded from :attr:`calib_dir` when it holds fitted parameters, else
        from the seed."""
        if self._models is None:
            models = self.family.build(self)
            for span, m in models.items():
                path = self.calib_dir / f"{span}.json" if self.calib_dir is not None else None
                m.load_or_default(path)
            self._models = models
        return self._models

    def predict(self, **feats) -> dict[str, float]:
        """The predicted duration of every span, in cycles (never negative), for *feats* -- which must
        be exactly the declared features."""
        self._check_features(feats)
        return {span: max(0.0, float(m.predict_feat(feats))) for span, m in self.tm.items()}

    def _check_features(self, feats: dict) -> None:
        missing = [f for f in self.features if f not in feats]
        extra = [f for f in feats if f not in self.features]
        if missing or extra:
            raise TypeError(f"phase {self.name!r} declares features {list(self.features)}; "
                            f"got {sorted(feats)} (missing {missing}, unexpected {extra})")

    # -- binding ---------------------------------------------------------------------------------
    def events(self) -> list[TimingEvent]:
        out: list[TimingEvent] = []
        for s, e in self.spans.values():
            out += [x for x in (s, e) if all(x is not y for y in out)]
        return out

    def _bind(self, owner) -> None:
        if self.owner is not None and self.owner is not owner:
            raise RuntimeError(f"phase {self.name!r} is already registered on "
                               f"'{getattr(self.owner, 'name', '?')}'")
        own = list(getattr(owner, "endpoints", {}).values())
        for ev in self.events():
            if all(ev.ep is not e for e in own) and getattr(ev.ep, "comp", None) is not owner:
                raise ValueError(
                    f"phase {self.name!r} on '{owner.name}': event {ev!r} is on an endpoint that is not "
                    f"the module's own.  A span uses only the module's own boundary endpoints; a span "
                    f"between two modules belongs to the composite that holds both.")
        self.owner = owner

    def _period(self, ep) -> float:
        clk = getattr(self.owner, "clk", None) or getattr(getattr(ep, "interface", None), "clk", None)
        if clk is None:
            raise RuntimeError(f"phase {self.name!r}: no clock -- the owner has no clk and the endpoint's "
                               f"interface none either")
        return float(clk.period)

    # -- Pattern 1 -------------------------------------------------------------------------------
    def timeout(self, **feats):
        """Pattern 1: wait this phase's one span, measured from its start event, and open its record;
        the next transfer on the span's end endpoint closes it.  Returns one SimPy event -- ``yield`` it.

        Raises if the start endpoint has no transfer this phase has not already used (the body did
        not do the transfer the declaration names), or if the features are not the declared ones."""
        if self.owner is None:
            raise RuntimeError(f"phase {self.name!r} is not registered: call the module's "
                               f"add_timing_phase(phase) in __post_init__")
        if len(self.spans) != 1:
            raise RuntimeError(f"phase {self.name!r} has {len(self.spans)} spans; timeout() waits one -- "
                               f"a pipelined phase runs through pipeline()")
        (span, (start, end)), = self.spans.items()
        self._check_features(feats)
        ep = start.ep
        log = ep.xfer_log
        used = self._used.get(ep.name, -1)
        if not log or log[-1][0] <= used:
            raise RuntimeError(
                f"phase {self.name!r}: span {span!r} starts at {start!r}, but '{ep.name}' has no "
                f"transfer since this phase last ran -- the body must complete a transfer on "
                f"'{ep.name}' before timeout()")
        index, first, last, _n, stalled = log[-1]
        self._used[ep.name] = index
        t_start = first if start.edge == "first" else last
        per = self._period(ep)
        dur = self.predict(**feats)[span]
        env = self.owner.env
        rec = PhaseRecord(self.name, span, self._occurrences, (ep.name, index, start.edge, t_start),
                          dict(feats), dur, stalled=bool(stalled))
        self._occurrences += 1
        self.records.append(rec)
        _open(self.owner).append((rec, end, per))
        return env.timeout(max(0.0, t_start + dur * per - env.now))


def _open(owner) -> list:
    """The owner's open phase records: ``(record, end event, clock period)``, oldest first."""
    lst = getattr(owner, "_open_phase_records", None)
    if lst is None:
        lst = []
        owner._open_phase_records = lst
    return lst


def note_transfer(ep, rec: tuple) -> None:
    """Called by an endpoint that carries a declared event when one of its transfers ends, with its
    log row ``(index, first, last, nwords, stalled)``: close every open record whose span ends here.

    A transfer on another of the owner's timed endpoints while a record is open is refused -- the span
    would then include waiting on that transfer, which the environment, not the module, decides
    (Rule 1)."""
    owner = getattr(ep, "_timing_owner", None)
    if owner is None:
        return
    still = []
    for item in _open(owner):
        r, end, per = item
        if end.ep is ep:
            index, first, last, _n, stalled = rec
            t_end = first if end.edge == "first" else last
            r.end = (ep.name, index, end.edge, t_end)
            r.actual = (t_end - r.start[3]) / per
            r.stalled = r.stalled or bool(stalled) or r.actual > r.predicted + 1e-6
        elif r.start[0] != ep.name:
            raise RuntimeError(
                f"phase {r.phase!r} on '{owner.name}': a transfer on '{ep.name}' ended while span "
                f"{r.span!r} was open -- it ends at {end!r}.  A span's duration must be the module's "
                f"own, not include waiting on another transfer")
        else:
            still.append(item)
    owner._open_phase_records = still


__all__ = ["EDGES", "LinearTiming", "PhaseRecord", "TimedPhase", "TimingEvent", "note_transfer"]
