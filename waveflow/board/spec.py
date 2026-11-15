"""Physical board targets and their named clock domains."""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .clock import ClockRequest, ClockResolution


def _check_frequency(name: str, value: float) -> None:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ValueError(f"{name} must be a positive finite frequency, got {value!r}")


@dataclass(frozen=True)
class ClockProfile:
    """One physical board clock and, when applicable, its HLS timing target."""

    physical_hz: float
    hls_target_hz: float | None

    def __post_init__(self) -> None:
        _check_frequency("physical_hz", self.physical_hz)
        if self.hls_target_hz is not None:
            _check_frequency("hls_target_hz", self.hls_target_hz)


@dataclass(frozen=True)
class ClockSource:
    """A board-owned generation recipe, distinct from a declared frequency profile."""

    generator: str
    reference_profile: str | None = None

    def __post_init__(self) -> None:
        if self.generator not in ("ps_pl0", "mmcm"):
            raise ValueError("clock generator must be 'ps_pl0' or 'mmcm'")
        if self.generator == "mmcm" and not self.reference_profile:
            raise ValueError("an MMCM source requires a reference_profile")
        if self.generator == "ps_pl0" and self.reference_profile is not None:
            raise ValueError("PS PL0 uses the board PS preset, not a PL reference profile")


@dataclass(frozen=True)
class Board:
    """A physical FPGA target, separate from a measured calibration platform."""

    name: str
    part: str
    board_part: str | None
    board_repo_dir: Path
    board_files_version: str
    vivado_version: str
    clocks: Mapping[str, ClockProfile]
    clock_sources: Mapping[str, ClockSource] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("board name must not be empty")
        if not self.part:
            raise ValueError("board part must not be empty")
        if not self.board_files_version:
            raise ValueError("board_files_version must not be empty")
        if not self.vivado_version:
            raise ValueError("vivado_version must not be empty")

        repo_dir = Path(self.board_repo_dir).resolve()
        clock_map = dict(self.clocks)
        if not clock_map:
            raise ValueError("a board must define at least one clock profile")
        if any(not name for name in clock_map):
            raise ValueError("clock profile names must not be empty")
        if any(not isinstance(profile, ClockProfile) for profile in clock_map.values()):
            raise TypeError("every clock profile must be a ClockProfile")

        source_map = dict(self.clock_sources)
        for name, source in source_map.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("clock source names must be nonempty strings")
            if not isinstance(source, ClockSource):
                raise TypeError("every clock source must be a ClockSource")
            if source.reference_profile is not None and source.reference_profile not in clock_map:
                raise ValueError(f"clock source {name!r} refers to an unknown reference profile")
        object.__setattr__(self, "clock_sources", MappingProxyType(source_map))
        object.__setattr__(self, "board_repo_dir", repo_dir)
        object.__setattr__(self, "clocks", MappingProxyType(clock_map))

    def clock(self, name: str) -> ClockProfile:
        """Return a named profile, with an error that lists the valid choices."""
        try:
            return self.clocks[name]
        except KeyError as exc:
            choices = ", ".join(sorted(self.clocks))
            raise ValueError(
                f"unknown clock profile {name!r} for board {self.name!r}; choose one of: {choices}"
            ) from exc

    def clock_source(self, name: str) -> ClockSource:
        """Look up a supported source without running a tool."""
        try:
            return self.clock_sources[name]
        except KeyError as exc:
            choices = ", ".join(sorted(self.clock_sources)) or "(none)"
            raise ValueError(
                f"unknown clock source {name!r} for board {self.name!r}; choose one of: {choices}"
            ) from exc

    def resolve_clock(
        self, request: ClockRequest, *, work_dir: str | Path | None = None,
    ) -> ClockResolution:
        """Explicitly invoke Vivado and return a tolerance-checked nominal clock.

        Each invocation uses a unique run directory under work_dir (or the system
        temporary directory). This does not validate a full BD, timing, or hardware.
        """
        from .clock import resolve_clock
        return resolve_clock(self, request, work_dir=work_dir)

    @property
    def board_files_dir(self) -> Path:
        """Directory containing this board revision's ``board.xml`` and companions."""
        return self.board_repo_dir / self.name / self.board_files_version

    @classmethod
    def rfsoc4x2(cls) -> Board:
        """Return the pinned RealDigital RFSoC 4x2 target shipped in the package."""
        repo_dir = Path(str(files("waveflow.board").joinpath("boards"))).resolve()
        board = cls(
            name="rfsoc4x2",
            part="xczu48dr-ffvg1517-2-e",
            board_part="realdigital.org:rfsoc4x2:part0:1.0",
            board_repo_dir=repo_dir,
            board_files_version="1.0",
            vivado_version="2025.1",
            clocks={
                "ps_control": ClockProfile(physical_hz=100e6, hls_target_hz=100e6),
                "rf_axis": ClockProfile(physical_hz=245.76e6, hls_target_hz=250e6),
                "pl_reference": ClockProfile(physical_hz=122.88e6, hls_target_hz=None),
                "adc_reference": ClockProfile(physical_hz=491.52e6, hls_target_hz=None),
                "dac_reference": ClockProfile(physical_hz=491.52e6, hls_target_hz=None),
            },
            clock_sources={
                "ps_pl0": ClockSource("ps_pl0"),
                "pl_ref_mmcm": ClockSource("mmcm", reference_profile="pl_reference"),
            },
        )
        required = (
            "LICENSE",
            "board.xml",
            "part0_pins.xml",
            "preset.xml",
            "rfsoc4x2_board.png",
            "xitem.json",
        )
        missing = [name for name in required if not (board.board_files_dir / name).is_file()]
        if missing:
            raise FileNotFoundError(
                f"packaged board files are incomplete at {board.board_files_dir}: "
                + ", ".join(missing)
            )
        return board
