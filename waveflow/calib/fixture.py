"""fixture.py — the per-component calibration fixture contract and registry.

A **platform** is retimed by running a *fixture* for every reusable infra component (the writer, the
reader, …).  A fixture is the component's own calibration harness: it declares what to sweep and how
to exercise the component standalone, and the engine turns that into a fitted residual under the
platform library.  This is the generalisation of what ``examples/mem_copy/calibrate_platform.py``
inlined for the one writer — hoisted into the package so it ships, and so a ``pip``-installed user can
recalibrate a platform that did not ship (:mod:`waveflow.calib.platform`).

Two pieces, both toolchain-free here (a concrete fixture's *RTL* side may need the toolchain, but the
contract, the registry, and the pysim vehicle do not):

* :class:`ComponentFixture` — subclass per component.  Declares ``component`` (the task-body id its
  firings carry, which keys the platform-library subdir), ``basis`` (the regression features), a
  :meth:`~ComponentFixture.sweep` grid, a :meth:`~ComponentFixture.run_pysim` that runs the component
  standalone for one point, and :meth:`~ComponentFixture.rtl_firings` (the measured/cosim RTL table
  for a point, or ``None`` when it must be measured).  :meth:`~ComponentFixture.calibrate` composes
  those into a :class:`~waveflow.calib.timing_model.StreamTimingModel` fit.
* the **registry** — fixtures self-register (:func:`register`) so a caller can enumerate "every
  infra component" (:func:`all_fixtures`) without a hardcoded list.  A ``retime`` command that
  sweeps them all to recalibrate a platform end to end is **planned, not built**; today the
  enumeration is there and the driving is per-example.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SweepPoint:
    """One point in a fixture's calibration grid — the structural facts of a firing.

    Parameters
    ----------
    label : str
        A short run id, unique within the sweep (the ``rtl/<label>`` / ``pysim/<label>`` folder name).
    features : dict[str, int]
        The regression-basis values at this point, e.g. ``{"nwords": 128, "num_trans": 8, "n_fwd": 1}``.
        Must cover the fixture's :attr:`~ComponentFixture.basis`.
    jobs : int
        How many firings to run at this point (more steady-state firings sharpen the median).  Advisory
        to :meth:`~ComponentFixture.run_pysim`.
    """
    label: str
    features: dict[str, int]
    jobs: int = 1


class ComponentFixture(ABC):
    """A reusable infra component's timing-calibration harness.

    Subclasses set the class attributes :attr:`component` and :attr:`basis` and implement
    :meth:`sweep` and :meth:`run_pysim` (and usually :meth:`rtl_firings`).
    """

    #: The task-body id this component's firings carry (e.g. ``"mem_w_stream_framed_done_task"``).  Keys
    #: the platform-library component subdir; must equal the id the RTL timing table uses.
    component: str = ""
    #: The regression basis — the features the residual is fit against, present on every sweep point.
    basis: list[str] = []

    def __init_subclass__(cls, **kw: Any) -> None:
        super().__init_subclass__(**kw)
        # A concrete (non-abstract) fixture must name its component and basis, or the registry key and
        # the fit are undefined.  Abstract intermediates are exempt.
        if not getattr(cls, "__abstractmethods__", None):
            if not cls.component:
                raise TypeError(f"{cls.__name__} must set `component` (the task-body id)")
            if not cls.basis:
                raise TypeError(f"{cls.__name__} must set `basis` (the regression features)")

    @abstractmethod
    def sweep(self) -> list[SweepPoint]:
        """The calibration grid.  Vary each feature *independently* (a grid, not a diagonal) — two
        features that move together are collinear and their coefficients cannot be separated."""

    @abstractmethod
    def run_pysim(self, point: SweepPoint, *, comp_dir, platform_dir, clk) -> list[dict]:
        """Run the component standalone for one *point* and return its ``firing_records``.

        ``comp_dir`` is where a :class:`~waveflow.calib.timing_model.StreamTimingModel` for this
        component lives on the platform (so pysim charges the current prediction); ``platform_dir`` is
        the platform root (so the memory can load the platform's bus law and the residual is the
        component's own control cost); ``clk`` is the platform clock.
        """

    def rtl_firings(self, point: SweepPoint) -> dict | None:
        """The RTL firing table for one *point*, as an ``ExtractBurstsStep`` events dict, or ``None``
        when it must be measured with the toolchain.

        Default ``None`` — a fixture whose RTL numbers come only from a live cosim.  A fixture with
        *measured* spans (or a committed table) overrides this to return them without the toolchain.
        """
        return None

    def timing_model(self, comp_dir, clk):
        """A :class:`~waveflow.calib.timing_model.StreamTimingModel` for this component at *comp_dir*,
        on this fixture's :attr:`basis`."""
        from waveflow.calib.timing_model import StreamTimingModel

        return StreamTimingModel(component=self.component, calib_dir=comp_dir,
                                 features=list(self.basis), clk=clk)

    def calibrate(self, platform, *, clk, reset: bool = True) -> dict:
        """Sweep the component, collect RTL + pysim into the platform's component dir, and fit.

        Returns a small report: the component, whether the model fitted, the fitted coefficients, and
        any sweep points whose RTL was not available (``rtl_firings`` returned ``None`` — they need a
        toolchain run and were skipped, not silently dropped)."""
        comp_dir = platform.component_dir(self.component)
        tm = self.timing_model(comp_dir, clk)
        if reset:
            tm.reset(corpus=True, params=True)

        missing_rtl: list[str] = []
        for pt in self.sweep():
            rtl = self.rtl_firings(pt)
            if rtl is None:
                missing_rtl.append(pt.label)
                continue
            tm.collect_rtl(rtl, run_id=pt.label)
            records = self.run_pysim(pt, comp_dir=comp_dir, platform_dir=platform.dir, clk=clk)
            tm.collect_pysim(records, run_id=pt.label)

        tm.fit()
        return {
            "component": self.component,
            "fitted": tm._model._fitted,
            "params": dict(tm._model.coeffs) if tm._model._fitted else {},
            "missing_rtl": missing_rtl,
            "coverage": tm.coverage,
        }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: "dict[str, ComponentFixture]" = {}


def register(fixture: ComponentFixture) -> ComponentFixture:
    """Register a fixture instance under its :attr:`~ComponentFixture.component` id.  Returns it, so a
    module can ``FIXTURE = register(MyFixture())`` at import.  A duplicate component id is an error —
    two fixtures fitting the same library slot is a bug, not an override."""
    key = fixture.component
    if key in _REGISTRY and _REGISTRY[key] is not fixture:
        raise ValueError(f"a fixture for component {key!r} is already registered "
                         f"({type(_REGISTRY[key]).__name__}); components map one-to-one to fixtures")
    _REGISTRY[key] = fixture
    return fixture


def get(component: str) -> ComponentFixture:
    """The registered fixture for *component*, or a ``KeyError`` naming what is registered."""
    try:
        return _REGISTRY[component]
    except KeyError:
        raise KeyError(f"no fixture registered for {component!r}; have {sorted(_REGISTRY)}") from None


def all_fixtures() -> "list[ComponentFixture]":
    """Every registered fixture, in registration order.

    The enumeration a whole-platform recalibration would iterate.  That command is planned, not
    built — see the module docstring."""
    return list(_REGISTRY.values())
