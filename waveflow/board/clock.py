"""Explicit, configuration-only Vivado clock resolution (no implementation or hardware test)."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from importlib.resources import files
from pathlib import Path
from types import MappingProxyType

from waveflow.toolchain import run_vivado

from .spec import Board, ClockProfile, _check_frequency


@dataclass(frozen=True)
class ClockRequest:
    """A physical request, with a separate optional HLS target.

    The source selects a board-owned recipe, not an arbitrary Vivado pin.
    Tolerance applies to the tool-calculated nominal physical frequency.
    """

    source: str
    requested_hz: float
    hls_target_hz: float | None = None
    tolerance_ppm: float = 100.0

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("clock source must be a nonempty name")
        _check_frequency("requested_hz", self.requested_hz)
        if self.hls_target_hz is not None:
            _check_frequency("hls_target_hz", self.hls_target_hz)
        if (
            isinstance(self.tolerance_ppm, bool)
            or not isinstance(self.tolerance_ppm, (int, float))
            or not math.isfinite(self.tolerance_ppm)
            or self.tolerance_ppm < 0
        ):
            raise ValueError("tolerance_ppm must be a nonnegative finite number")


class ClockProbeError(RuntimeError):
    """The tool or its output could not establish a usable clock configuration."""


class ClockRequestError(ValueError):
    """A candidate outside tolerance; inspect the 'resolution' attribute."""

    def __init__(self, resolution: ClockResolution) -> None:
        self.resolution = resolution
        super().__init__(
            f"clock source {resolution.request.source!r}: requested "
            f"{resolution.requested_hz:g} Hz, calculated {resolution.actual_hz:g} Hz; "
            f"error {resolution.error_ppm:g} ppm exceeds tolerance "
            f"{resolution.request.tolerance_ppm:g} ppm. Log: {resolution.log_path}"
        )


@dataclass(frozen=True)
class ClockResolution:
    """A candidate profile, request, recipe identity, and retained evidence.

    Rejected candidates cannot drive BuildConfig. This is configuration evidence,
    not a measured board clock or a guarantee of implementation timing.
    """

    request: ClockRequest
    profile: ClockProfile
    board_fingerprint: str
    generator_parameters: Mapping[str, str | float | int]
    work_dir: Path
    resolved_at: str
    validation_level: str = "ip_configuration"

    def __post_init__(self) -> None:
        if not isinstance(self.request, ClockRequest) or not isinstance(self.profile, ClockProfile):
            raise TypeError("resolution requires a ClockRequest and ClockProfile")
        if self.profile.hls_target_hz != self.request.hls_target_hz:
            raise ValueError("resolution HLS target differs from its request")
        if self.validation_level != "ip_configuration":
            raise ValueError("unsupported clock validation level")
        object.__setattr__(self, "generator_parameters",
                           MappingProxyType(dict(self.generator_parameters)))
        object.__setattr__(self, "work_dir", Path(self.work_dir).resolve())

    @property
    def requested_hz(self) -> float:
        return self.request.requested_hz

    @property
    def actual_hz(self) -> float:
        return self.profile.physical_hz

    @property
    def error_ppm(self) -> float:
        return abs(self.actual_hz - self.requested_hz) / self.requested_hz * 1e6

    @property
    def accepted(self) -> bool:
        return self.error_ppm <= self.request.tolerance_ppm

    @property
    def log_path(self) -> Path:
        return self.work_dir / "vivado.log"

    @property
    def manifest_path(self) -> Path:
        return self.work_dir / "clock_resolution.json"

    def require_compatible(self, board: Board) -> None:
        """Check compatibility without launching Vivado or changing the board."""
        if not self.accepted:
            raise ClockRequestError(self)
        if self.board_fingerprint != _context(board, self.request.source)[1]:
            raise ValueError("resolved clock does not match board, clock source, or recipe version")


def _recipe_path() -> Path:
    return Path(str(files("waveflow.board").joinpath("tcl", "resolve_clock.tcl")))


def _context(board: Board, source_name: str) -> tuple[dict, str]:
    source = board.clock_source(source_name)
    if not board.board_part:
        raise ValueError("clock resolution requires a board_part and packaged board preset")
    root = board.board_files_dir
    for name in ("board.xml", "preset.xml", "part0_pins.xml"):
        if not (root / name).is_file():
            raise FileNotFoundError(f"clock resolution requires board file: {root / name}")
    board_hashes = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*")) if path.is_file()
    }
    context = {
        "schema_version": 1,
        "board_name": board.name,
        "part": board.part,
        "board_part": board.board_part,
        "board_files_version": board.board_files_version,
        "board_file_hashes": board_hashes,
        "vivado_version": board.vivado_version,
        "source": source_name,
        "generator": source.generator,
        "reference_profile": source.reference_profile,
        "input_hz": (
            board.clock(source.reference_profile).physical_hz
            if source.reference_profile is not None else None
        ),
        "recipe_sha256": hashlib.sha256(_recipe_path().read_bytes()).hexdigest(),
    }
    fingerprint = hashlib.sha256(json.dumps(context, sort_keys=True).encode()).hexdigest()
    return context, fingerprint


def _number(record: dict, name: str) -> float:
    value = record.get(name)
    try:
        _check_frequency(name, value)
    except ValueError as exc:
        raise ClockProbeError(f"invalid clock result: {name}={value!r}") from exc
    return float(value)


def _parse_output(stdout: str, board: Board, request: ClockRequest, context: dict) -> dict:
    lines = stdout.splitlines()
    if "WAVEFLOW_CLOCK_OK" not in lines:
        raise ClockProbeError("Vivado did not report successful clock resolution")
    prefix = "WAVEFLOW_CLOCK_RESULT: "
    payloads = [line[len(prefix):] for line in lines if line.startswith(prefix)]
    if len(payloads) != 1:
        raise ClockProbeError("expected exactly one clock result from Vivado")
    try:
        record = json.loads(payloads[0])
    except (ValueError, TypeError) as exc:
        raise ClockProbeError("invalid clock result JSON") from exc
    if not isinstance(record, dict):
        raise ClockProbeError("clock result must be an object")
    for key, expected in (
        ("part", board.part), ("board_part", board.board_part),
        ("vivado_version", board.vivado_version), ("source", request.source),
        ("generator", context["generator"]),
    ):
        if record.get(key) != expected:
            raise ClockProbeError(f"clock result {key} does not match the request")
    reported_request = _number(record, "requested_hz")
    if not math.isclose(reported_request, request.requested_hz, rel_tol=1e-12, abs_tol=0):
        raise ClockProbeError("Vivado reported a different requested frequency")
    actual = _number(record, "actual_hz")
    pin_hz = _number(record, "pin_hz")
    if abs(actual - pin_hz) > 1.0:
        raise ClockProbeError("calculated frequency disagrees with clock pin metadata")
    parameters = record.get("parameters")
    if not isinstance(parameters, dict):
        raise ClockProbeError("clock generator parameters are missing")
    if context["generator"] == "ps_pl0":
        if parameters.get("clock_source") not in ("IOPLL", "RPLL", "DPLL"):
            raise ClockProbeError("invalid PS clock source")
        _number(parameters, "reference_hz")
        for key in ("divisor0", "divisor1"):
            divisor = _number(parameters, key)
            if not divisor.is_integer() or not 1 <= divisor <= 63:
                raise ClockProbeError(f"invalid PS {key}")
    else:
        fin = _number(parameters, "input_hz")
        if not math.isclose(fin, context["input_hz"], rel_tol=1e-12):
            raise ClockProbeError("MMCM input frequency differs from the board reference")
        m = _number(parameters, "multiplier")
        d = _number(parameters, "input_divider")
        o = _number(parameters, "output_divider")
        if not math.isclose(fin * m / d / o, actual, rel_tol=1e-12, abs_tol=1e-6):
            raise ClockProbeError("MMCM parameters disagree with its calculated frequency")
    return record


def resolve_clock(
    board: Board, request: ClockRequest, *, work_dir: str | Path | None = None,
) -> ClockResolution:
    """Run a fresh single-output probe in a unique directory under work_dir.

    No cache or recorded-results fallback is used. Every call invokes the pinned
    Vivado release. Named profile lookup and Board construction remain tool-free.
    """
    if not isinstance(request, ClockRequest):
        raise TypeError("request must be a ClockRequest")
    context, fingerprint = _context(board, request.source)
    parent = None
    if work_dir is not None:
        parent = Path(work_dir).resolve()
        parent.mkdir(parents=True, exist_ok=True)
    run_dir = Path(tempfile.mkdtemp(prefix="waveflow-clock-", dir=parent))
    script = run_dir / "resolve_clock.tcl"
    shutil.copyfile(_recipe_path(), script)
    try:
        completed = run_vivado(
            script, run_dir,
            args=[
                board.board_repo_dir, board.board_part, board.part,
                request.source, context["generator"], format(request.requested_hz, ".17g"),
                format(context["input_hz"] or 0, ".17g"),
            ],
            required_version=board.vivado_version,
        )
        record = _parse_output(completed.stdout or "", board, request, context)
    except (subprocess.CalledProcessError, RuntimeError) as exc:
        raise ClockProbeError(f"clock probe failed; inspect {run_dir / 'vivado.log'}: {exc}") from exc
    if _context(board, request.source)[1] != fingerprint:
        raise ClockProbeError("board files or clock recipe changed during the probe")
    resolution = ClockResolution(
        request=request,
        profile=ClockProfile(record["actual_hz"], request.hls_target_hz),
        board_fingerprint=fingerprint,
        generator_parameters=record["parameters"],
        work_dir=run_dir,
        resolved_at=datetime.now(timezone.utc).isoformat(),
    )
    manifest = {
        "context": context,
        "board_fingerprint": fingerprint,
        "request": asdict(request),
        "profile": asdict(resolution.profile),
        "error_ppm": resolution.error_ppm,
        "accepted": resolution.accepted,
        "validation_level": resolution.validation_level,
        "resolved_at": resolution.resolved_at,
        "tool_result": record,
        "log_file": "vivado.log",
        "design_file": ".srcs/sources_1/bd/clock_resolution/clock_resolution.bd",
    }
    resolution.manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    if not resolution.accepted:
        raise ClockRequestError(resolution)
    return resolution
