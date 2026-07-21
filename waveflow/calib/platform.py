"""platform.py — the per-platform calibration library.

A platform (board / BFM) owns two kinds of *reusable* timing calibration, both fit **once** and reused
by every accelerator built on it:

* the ``m_axi`` **bus-transfer** law (:class:`~waveflow.calib.bus_model.BusCalib`), and
* the **control residual** of each reusable infra component (``MemRStream`` / ``MemWStream`` and any
  future framework kernel) — a property of ``(component, platform)``, not of the one accelerator that
  happens to compose it.

:class:`PlatformCalib` is the directory *layout* that holds both, so "calibrate the platform once" is a
single directory a new project points at::

    <platform_dir>/
        mm_bus.json           # BusCalib: the bus-transfer span law
        points/               # BusCalib corpus (distilled {num_trans, nwords, span} rows)
        components/<comp>/     # a reusable component's residual: params.json + rtl/ pysim/ corpus.csv

Contrast with a project-local ``calib_dir`` (what a one-off accelerator passes to its own
:class:`~waveflow.calib.timing_model.TimingModel`): that lands the residual in the *project* tree.  The
platform library is the shared home an **infra** component's residual moves to once it is calibrated, so
the *next* project loads it (:meth:`component_dir` → its ``params.json``) without a toolchain run —
exactly the reuse ``BusCalib`` already gives the bus law.  The unit that is checked in is the same for
both: the distilled corpus (KB) plus the fitted params, never the raw traces.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from waveflow.calib.bus_model import BusCalib

#: Subdirectory under a platform dir holding the per-component residual libraries.
COMPONENTS_SUBDIR = "components"


@dataclass
class PlatformCalib:
    """The shared calibration library for one platform — the home for the bus law and every reusable
    component's residual.

    Parameters
    ----------
    platform_dir : str | Path
        The platform's calibration directory (board / BFM scope), shared across accelerators.
    clk_freq : float
        Clock the stored models are expressed against — passed through to the :class:`BusCalib` it
        hands back (the component residuals are clock-independent cycles, resolved by each component's
        own ``clk``).
    """

    platform_dir: str | Path
    clk_freq: float = 100e6

    def component_dir(self, component: str) -> Path:
        """The shared calibration directory for a reusable component on this platform, keyed by the
        component identity (its task-body id, e.g. ``"mem_w_stream_framed_done_task"``).

        A :class:`~waveflow.calib.timing_model.StreamTimingModel` points its ``calib_dir`` here so the
        fit is stored *with the platform* — the residual then reloads in any project that composes the
        same component on the same platform, no recalibration."""
        return Path(self.platform_dir) / COMPONENTS_SUBDIR / str(component)

    @property
    def bus(self) -> BusCalib:
        """The platform's bus-transfer calibrator, rooted at the same directory (writes/reads
        ``mm_bus.json`` + ``points/`` alongside ``components/``)."""
        return BusCalib(platform_dir=self.platform_dir, clk_freq=self.clk_freq)
