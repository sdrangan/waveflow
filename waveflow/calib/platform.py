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
import os
import warnings
from dataclasses import dataclass
from pathlib import Path

from waveflow.calib.bus_model import BusCalib

#: Subdirectory under a platform dir holding the per-component residual libraries.
COMPONENTS_SUBDIR = "components"

#: Environment variable naming extra platform roots to search first, ``os.pathsep``-separated.  The
#: escape hatch for pointing a build at a platform library outside the standard locations (CI, a
#: shared network dir, a checkout).
PLATFORM_PATH_ENV = "WAVEFLOW_PLATFORM_PATH"


def user_platforms_dir() -> Path:
    """The per-user, writable platform library — where :mod:`waveflow.calib.retime` lands a platform
    a user calibrates that did not ship with the package.

    A ``pip``-installed user cannot write into ``site-packages``, so a recalibration for a new board
    needs a writable home outside the wheel.  This is the OS-conventional user-data location
    (``~/.local/share/waveflow/platforms`` on Linux, the equivalent elsewhere)."""
    import platformdirs

    # appauthor=False drops the vendor segment Windows would otherwise insert (…/waveflow/waveflow).
    return Path(platformdirs.user_data_dir("waveflow", appauthor=False)) / "platforms"


def packaged_platforms_dir() -> Path | None:
    """The read-only reference platforms shipped inside the ``waveflow`` package, or ``None`` if they
    are not resolvable as a real directory (e.g. a zip-imported install).

    These are the calibrations Waveflow ships (``zynq7020_bfm_100mhz`` today): available to every
    installed user with no checkout, and the last-resort fallback of :func:`platform_fallback_path`."""
    try:
        from importlib.resources import files
        trav = files("waveflow.calib").joinpath("platforms")
    except (ModuleNotFoundError, AttributeError):     # pragma: no cover - defensive
        return None
    p = Path(str(trav))
    return p if p.is_dir() else None


def platform_fallback_path() -> list[Path]:
    """The read-only roots to search *after* a build's own ``platforms_root``, most-preferred first.

    Order: any :data:`PLATFORM_PATH_ENV` entries, then the per-user library
    (:func:`user_platforms_dir`), then the packaged reference (:func:`packaged_platforms_dir`).  A
    platform a user calibrated locally therefore shadows the packaged one of the same name, and the
    shipped reference is always resolvable as the final fallback.  Duplicate roots are dropped,
    preserving order."""
    roots: list[Path] = []
    env = os.environ.get(PLATFORM_PATH_ENV)
    if env:
        roots += [Path(p) for p in env.split(os.pathsep) if p]
    roots.append(user_platforms_dir())
    pkg = packaged_platforms_dir()
    if pkg is not None:
        roots.append(pkg)
    seen: set[Path] = set()
    ordered: list[Path] = []
    for r in roots:
        key = Path(os.path.normcase(os.path.abspath(r)))
        if key not in seen:
            seen.add(key)
            ordered.append(r)
    return ordered

#: The per-platform identity manifest — the FPGA part + clock the calibration was fit against.  This
#: is the ONE source both the synthesis TCL (``set_part`` / ``create_clock``) and the calibration
#: load/store read, so the synthesised part can never drift from the calibrated part.
PLATFORM_MANIFEST = "platform.json"


#: The Vitis / FPGA counter vocabulary — the default when a platform does not declare its own.
#: Deliberately named for the technology it describes: it is not a universal set, and an ASIC flow
#: measures different things (cell area, macro count) in different units (a float, not a count).
VITIS_RES_TYPES: "tuple[str, ...]" = ("lut", "ff", "dsp", "bram", "uram", "srl")


class UnknownCounterError(ValueError):
    """A model named a resource counter the platform does not measure in.

    Fatal rather than ignored: an unknown counter is dropped when counters are summed, so the module
    silently contributes zero and the design reads as cheaper than it is.
    """


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
    #: The resource counters this technology is measured in.  ``None`` means the Vitis/FPGA default
    #: (:data:`VITIS_RES_TYPES`) — see :attr:`res_types`.
    res_types_stored: "tuple[str, ...] | None" = None

    @property
    def res_types(self) -> "tuple[str, ...]":
        """The counter vocabulary for this platform, defaulting to the Vitis/FPGA set.

        A counter set is exactly as technology-specific as ``part`` and ``clk_freq``, so it belongs on
        the same object rather than as a module-level constant: an ASIC flow counts cell area and macro
        instances, not LUTs and BRAMs, and it should enter by *declaring a platform* rather than by
        reworking the model layer.  ``None`` keeps the FPGA default, so every platform written before
        this existed loads unchanged.
        """
        return tuple(self.res_types_stored) if self.res_types_stored else VITIS_RES_TYPES

    def check_counters(self, names) -> None:
        """Raise :class:`UnknownCounterError` for any name outside this platform's vocabulary.

        The guard that makes the vocabulary real rather than advisory.  Without it a mistyped counter
        predicts fine in isolation and is silently dropped when summed — so the module contributes
        **zero**, and a missing contribution makes a design look *cheaper* than it is, which turns
        "does not fit" into "fits".
        """
        unknown = sorted(set(map(str, names)) - set(self.res_types))
        if unknown:
            raise UnknownCounterError(
                f"unknown resource counter(s) {unknown} for platform {self.name!r}; it is measured in "
                f"{list(self.res_types)}.  A counter outside the vocabulary is silently dropped when "
                f"counters are summed, so this is refused rather than ignored."
            )

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
                clk_freq: float | None = None, allow_mismatch: bool = False,
                fallbacks: "list[str | Path] | None" = None,
                res_types: "tuple[str, ...] | None" = None) -> "Platform":
        """Find platform *name* under *platforms_root* (then the *fallbacks*), or create it.

        Resolution searches ``[platforms_root, *fallbacks]`` in order for an existing platform:

        * **Found** (a ``platform.json``): load the stored manifest and **confirm** the build's
          part/clock match it.  A mismatch raises :class:`PlatformMismatchError` unless *allow_mismatch*,
          which downgrades it to a :class:`PlatformMismatchWarning`.  The stored values (what the fit is
          valid for) win.  A platform in an earlier root shadows a same-named one later — so a user's
          locally calibrated platform takes precedence over the packaged reference.
        * **Absent everywhere**: create it in ``platforms_root`` (the primary, always the write target)
          and seed the manifest with the build's ``part`` / ``clk_freq`` — ready to be populated by a
          sweep + publish.

        *fallbacks* are **read-only** search roots (typically :func:`platform_fallback_path`: the
        ``WAVEFLOW_PLATFORM_PATH`` env, the per-user library, and the packaged reference).  ``None``
        preserves the single-root behaviour — resolution and creation both act on ``platforms_root``
        alone.
        """
        roots = [Path(platforms_root)] + [Path(f) for f in (fallbacks or [])]
        for root in roots:
            pdir = root / name
            manifest = pdir / PLATFORM_MANIFEST
            if not manifest.is_file():
                continue
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
            return cls(name=name, dir=pdir, part=stored_part, clk_freq=stored_freq,
                       res_types_stored=tuple(data["res_types"]) if data.get("res_types") else None)

        # Not found in any root — create it in the primary (never a read-only fallback).
        pdir = Path(platforms_root) / name
        pdir.mkdir(parents=True, exist_ok=True)
        manifest = {"part": part, "clk_freq_hz": clk_freq}
        if res_types:
            # Written only when it differs from the FPGA default, so an ordinary Vitis platform's
            # manifest is byte-identical to what it was before this existed.
            manifest["res_types"] = list(res_types)
        (pdir / PLATFORM_MANIFEST).write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        return cls(name=name, dir=pdir, part=part, clk_freq=clk_freq,
                   res_types_stored=tuple(res_types) if res_types else None)


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
