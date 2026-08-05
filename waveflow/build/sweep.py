"""sweep.py — run a design at every point of a parameter grid, and keep the measurements.

A calibration corpus is built by sweeping: elaborate a design at each point of a grid, run it through
the build DAG, and let the steps file what they measured.  Two examples did that with ~150 lines
each, of which about fifteen were about the design; a third (``examples/mem_copy``) did it again for
timing.  The rest was the same ten concerns written three times -- config construction, failure
isolation, incremental save, resume, dry-run, progress, exit code.

Not modelled on ``GridSearchCV``, despite the surface resemblance.  That is an *optimizer*: it
cross-validates, scores, and hands back ``best_params_``.  A synthesis sweep is a **census** -- it
measures every point to build a corpus, and there is no score to maximize and no held-out fold at
sweep time.  The right analogue is ``ParameterGrid``, the iterator, plus a runner owning what sklearn
has no concept of because its estimators fit in milliseconds while these points cost ~45 seconds of
Vitis each.

See ``plans/sweep_runner.md``.
"""
from __future__ import annotations

from itertools import product
from typing import Any, Iterator, Mapping, Sequence


class ParamGrid:
    """The points a sweep visits: a Cartesian product over named axes, in **declaration order**.

    ```python
    ParamGrid(vlen=(512, 1024), dwid=(32, 64))
    # vlen is the OUTER loop -- {vlen:512,dwid:32}, {vlen:512,dwid:64}, {vlen:1024,dwid:32}, ...
    ```

    Declaration order *is* iteration order, so a grid reads the way it runs.  That matters more than
    it looks: the two existing sweeps disagree about it (one iterates its first-named axis fastest and
    the other slowest), and a runner that imposed its own order would silently reorder an interrupted
    sweep's remaining work.

    **A single-value axis is a constant.**  ``samp_i=(2,)`` contributes one value to every point and no
    branching, which is how a design that holds a parameter fixed says so without a second concept for
    it.
    """

    def __init__(self, _workload: "Sequence[str]" = (), **axes: "Sequence[Any]") -> None:
        empty = [k for k, v in axes.items() if not len(tuple(v))]
        if empty:
            raise ValueError(
                f"axis {empty} has no values; a grid with an empty axis yields no points at all, "
                f"which is almost never what was meant — pass a single-value tuple for a constant")
        unknown = [w for w in _workload if w not in axes]
        if unknown:
            raise ValueError(f"_workload names {unknown}, which are not axes of this grid")
        self.axes: dict = {k: tuple(v) for k, v in axes.items()}
        #: Axes that vary the **workload** rather than the hardware.
        #:
        #: A build axis is a ``HwParam``: changing it produces different hardware, so every stage from
        #: elaboration onward must re-run.  A workload axis is a runtime input -- the hardware is
        #: unchanged and only the simulation repeats.  Utilization does not depend on workload at all
        #: (``ResourceModel.get_params`` drops ``**runtime`` for exactly that reason), while a timing
        #: corpus is mostly workload points against one build.  A runner that could not tell them
        #: apart would re-synthesize for a change in ``nwords``, turning a seconds-long pysim sweep
        #: into an hours-long one.
        self.workload: frozenset = frozenset(_workload)

    @property
    def build_axes(self) -> dict:
        """Axes whose change requires re-elaboration and re-synthesis."""
        return {k: v for k, v in self.axes.items() if k not in self.workload}

    @property
    def workload_axes(self) -> dict:
        return {k: v for k, v in self.axes.items() if k in self.workload}

    def __len__(self) -> int:
        n = 1
        for v in self.axes.values():
            n *= len(v)
        return n

    def __iter__(self) -> "Iterator[dict]":
        names = list(self.axes)
        for combo in product(*(self.axes[n] for n in names)):
            yield dict(zip(names, combo))

    def label(self, point: "Mapping[str, Any]") -> str:
        """A filesystem- and log-safe name for one point.

        Derived from the point rather than written per example, so the summary key and the progress
        line cannot disagree about which point a record belongs to.  Only the axes appear -- a
        constant contributes nothing to distinguishing points and would only pad every label.
        """
        varying = [k for k, v in self.axes.items() if len(v) > 1]
        parts = [f"{k}{_slug(point[k])}" for k in (varying or list(self.axes)) if k in point]
        return "_".join(parts) or "point"

    def subset(self, **overrides: "Sequence[Any] | None") -> "ParamGrid":
        """A grid with some axes restricted — what a ``--ntap 8 16`` flag produces.

        ``None`` leaves an axis alone, so a CLI can pass every axis through without special-casing
        the ones the user did not mention.
        """
        axes = dict(self.axes)
        for k, v in overrides.items():
            if v is None:
                continue
            if k not in axes:
                raise ValueError(f"{k!r} is not an axis of this grid ({sorted(axes)})")
            axes[k] = tuple(v)
        return ParamGrid(_workload=tuple(self.workload), **axes)

    def __repr__(self) -> str:
        inner = ", ".join(f"{k}={v!r}" for k, v in self.axes.items())
        return f"ParamGrid({inner})"


def _slug(value: Any) -> str:
    """A label fragment: ``True``/``False`` read better than ``1``/``0`` in a sweep log."""
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value).replace(" ", "").replace("/", "-")


__all__ = ["ParamGrid"]
