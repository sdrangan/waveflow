"""Physical board targets, reference clocks and available generation routes."""

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
class ReferenceClock:
    """A nominal board input, with its physical source and consumers.

    Frequencies from programmable devices are documented defaults, not measured
    values. Look up ``source`` in ``Board.clock_devices`` for setup requirements.
    """

    frequency_hz: float
    source: str = ""
    output: str = ""
    consumers: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _check_frequency("frequency_hz", self.frequency_hz)
        object.__setattr__(self, "consumers", tuple(self.consumers))


@dataclass(frozen=True)
class ClockDevice:
    """Clock hardware and its inputs; this inventory does not implement a driver.

    Internal PS PLLs are included alongside external devices. No output target
    frequency is assigned here. ``consumers`` lists selected preset connections.
    """

    model: str
    reference_clocks: tuple[str, ...] = ()
    configuration: str = "fixed"
    consumers: tuple[str, ...] = ()
    notes: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.model, str) or not self.model.strip():
            raise ValueError("clock device requires a model")
        if self.configuration not in ("fixed", "factory_rom", "ps_preset", "runtime_spi"):
            raise ValueError("unknown clock device configuration method")
        object.__setattr__(self, "reference_clocks", tuple(self.reference_clocks))
        object.__setattr__(self, "consumers", tuple(self.consumers))


@dataclass(frozen=True)
class ClockProfile:
    """A resolved output frequency and its separate optional HLS timing target."""

    physical_hz: float
    hls_target_hz: float | None

    def __post_init__(self) -> None:
        _check_frequency("physical_hz", self.physical_hz)
        if self.hls_target_hz is not None:
            _check_frequency("hls_target_hz", self.hls_target_hz)


@dataclass(frozen=True)
class ClockSource:
    """A board-owned generation route from one available reference clock."""

    generator: str
    reference_clock: str

    def __post_init__(self) -> None:
        if self.generator not in ("ps_pl0", "mmcm"):
            raise ValueError("clock generator must be 'ps_pl0' or 'mmcm'")
        if not isinstance(self.reference_clock, str) or not self.reference_clock.strip():
            raise ValueError("a clock source requires a reference_clock")


@dataclass(frozen=True)
class Board:
    """A physical FPGA target, separate from a measured calibration platform."""

    name: str
    part: str
    board_part: str | None
    board_repo_dir: Path
    board_files_version: str
    vivado_version: str
    reference_clocks: Mapping[str, ReferenceClock]
    clock_sources: Mapping[str, ClockSource] = field(default_factory=dict)
    clock_devices: Mapping[str, ClockDevice] = field(default_factory=dict)

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
        clock_map = dict(self.reference_clocks)
        if not clock_map:
            raise ValueError("a board must define at least one reference clock")
        if any(not name for name in clock_map):
            raise ValueError("reference clock names must not be empty")
        if any(not isinstance(clock, ReferenceClock) for clock in clock_map.values()):
            raise TypeError("every board clock must be a ReferenceClock")

        device_map = dict(self.clock_devices)
        for name, device in device_map.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("clock device names must be nonempty strings")
            if not isinstance(device, ClockDevice):
                raise TypeError("every clock device must be a ClockDevice")
            if any(ref not in clock_map for ref in device.reference_clocks):
                raise ValueError(f"clock device {name!r} refers to an unknown reference clock")
        for name, clock in clock_map.items():
            if clock.source and clock.source not in device_map:
                raise ValueError(f"reference clock {name!r} refers to an unknown clock device")

        source_map = dict(self.clock_sources)
        for name, source in source_map.items():
            if not isinstance(name, str) or not name.strip():
                raise ValueError("clock source names must be nonempty strings")
            if not isinstance(source, ClockSource):
                raise TypeError("every clock source must be a ClockSource")
            if source.reference_clock not in clock_map:
                raise ValueError(f"clock source {name!r} refers to an unknown reference clock")
        object.__setattr__(self, "clock_sources", MappingProxyType(source_map))
        object.__setattr__(self, "clock_devices", MappingProxyType(device_map))
        object.__setattr__(self, "board_repo_dir", repo_dir)
        object.__setattr__(self, "reference_clocks", MappingProxyType(clock_map))

    def reference_clock(self, name: str) -> ReferenceClock:
        """Look up a board reference without running a tool or choosing an output."""
        try:
            return self.reference_clocks[name]
        except KeyError as exc:
            choices = ", ".join(sorted(self.reference_clocks))
            raise ValueError(
                f"unknown reference clock {name!r} for board {self.name!r}; choose one of: {choices}"
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
        from .rfsoc4x2 import clock_inventory

        repo_dir = Path(str(files("waveflow.board").joinpath("boards"))).resolve()
        references, devices = clock_inventory()
        board = cls(
            name="rfsoc4x2",
            part="xczu48dr-ffvg1517-2-e",
            board_part="realdigital.org:rfsoc4x2:part0:1.0",
            board_repo_dir=repo_dir,
            board_files_version="1.0",
            vivado_version="2025.1",
            reference_clocks=references,
            clock_devices=devices,
            clock_sources={
                "ps_pl0": ClockSource("ps_pl0", reference_clock="ps_reference"),
                "pl_ref_mmcm": ClockSource("mmcm", reference_clock="pl_reference"),
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
