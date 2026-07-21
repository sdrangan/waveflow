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

import json
import math
import warnings
from dataclasses import dataclass
from pathlib import Path

from waveflow.calib.bus_model import BusCalib

#: Subdirectory under a platform dir holding the per-component residual libraries.
COMPONENTS_SUBDIR = "components"

#: The per-platform identity manifest — the FPGA part + clock the calibration was fit against.  This
#: is the ONE source both the synthesis TCL (``set_part`` / ``create_clock``) and the calibration
#: load/store read, so the synthesised part can never drift from the calibrated part.
PLATFORM_MANIFEST = "platform.json"


class PlatformMismatchError(RuntimeError):
    """A selected platform's stored ``part`` / clock does not match the build's, and the build did not
    set ``allow_platform_mismatch``.  Cycle-level calibration is only valid for the part+clock it was
    fit against (HLS schedules to the target period; primitive latencies differ across families), so a
    mismatch is fatal unless explicitly allowed."""


class PlatformMismatchWarning(UserWarning):
    """Emitted (instead of raised) when a platform's part/clock differs from the build's but
    ``allow_platform_mismatch`` is set — the reused fit may not reproduce this part's cycle counts."""


@dataclass
class Platform:
    """A named calibration platform: a directory plus the ``part`` + ``clk_freq`` its fit is valid for.

    Resolved from a :class:`~waveflow.build.build.BuildConfig` via :meth:`resolve`, which is the
    create-or-confirm gate — a new platform gets an empty dir seeded with the build's part/clock; an
    existing one has its stored part/clock **confirmed** against the build's.
    """

    name: str
    dir: Path
    part: str | None = None
    clk_freq: float | None = None      # Hz — the synthesis clock (its period drives HLS scheduling)

    @property
    def manifest_path(self) -> Path:
        return self.dir / PLATFORM_MANIFEST

    @property
    def synth_period_ns(self) -> float | None:
        """The ``create_clock -period`` (ns) the TCL should emit — the reciprocal of ``clk_freq``."""
        return None if not self.clk_freq else 1e9 / self.clk_freq

    def calib(self) -> BusCalib:
        """The platform's :class:`~waveflow.calib.bus_model.BusCalib` (bus law), rooted at this dir."""
        return PlatformCalib(self.dir, clk_freq=self.clk_freq or 100e6).bus

    def component_dir(self, component: str) -> Path:
        return PlatformCalib(self.dir).component_dir(component)

    @classmethod
    def resolve(cls, platforms_root: str | Path, name: str, *, part: str | None = None,
                clk_freq: float | None = None, allow_mismatch: bool = False) -> "Platform":
        """Find platform *name* under *platforms_root*, or create it.

        * **Absent** (no ``platform.json``): create the directory and seed the manifest with the build's
          ``part`` / ``clk_freq`` — the platform now exists, ready to be populated by a sweep + publish.
        * **Present**: load the stored manifest and **confirm** the build's part/clock match it.  A
          mismatch raises :class:`PlatformMismatchError` unless *allow_mismatch*, which downgrades it to
          a :class:`PlatformMismatchWarning`.  The stored values (what the fit is valid for) win.
        """
        pdir = Path(platforms_root) / name
        manifest = pdir / PLATFORM_MANIFEST
        if manifest.is_file():
            data = json.loads(manifest.read_text(encoding="utf-8"))
            stored_part = data.get("part")
            stored_freq = data.get("clk_freq_hz")
            diffs = []
            if part is not None and stored_part is not None and part != stored_part:
                diffs.append(f"part: build {part!r} != platform {stored_part!r}")
            if (clk_freq is not None and stored_freq is not None
                    and not math.isclose(clk_freq, stored_freq, rel_tol=1e-9)):
                diffs.append(f"clk_freq: build {clk_freq} != platform {stored_freq}")
            if diffs:
                msg = (f"platform {name!r} was calibrated for a different target — "
                       + "; ".join(diffs) + ". Cycle counts may not reproduce.")
                if not allow_mismatch:
                    raise PlatformMismatchError(
                        msg + " Set allow_platform_mismatch=True to reuse it anyway.")
                warnings.warn(msg, PlatformMismatchWarning, stacklevel=2)
            return cls(name=name, dir=pdir, part=stored_part, clk_freq=stored_freq)

        pdir.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps({"part": part, "clk_freq_hz": clk_freq}, indent=2) + "\n", encoding="utf-8")
        return cls(name=name, dir=pdir, part=part, clk_freq=clk_freq)


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
