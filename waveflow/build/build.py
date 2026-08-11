"""
build.py — build configuration, pipeline primitives, and artifact generation.

Classes
-------
BuildConfig
    Dataclass holding paths and tool settings for a build.

BuildArtifact  (ABC)
    A single named output from a step — either a file on disk or an in-memory
    Python object.

FileArtifact  (BuildArtifact)
    A file written to disk; freshness is checked via the file's mtime.

ObjectArtifact  (BuildArtifact)
    An in-memory Python object; freshness is checked via creation timestamp.

BuildResult
    Dataclass returned by every ``BuildStep.run()`` call.

BuildStep  (ABC)
    Abstract base for any node in a build DAG.  Declares ``description``,
    ``consumes``, ``produces``, and ``params`` as class-level attributes that
    subclasses override.  ``run()`` receives injected consumed artifacts and
    resolved params as kwargs and returns a dict of produced artifacts.

Buildable  (ABC, extends BuildStep)
    Legacy base for steps that declare named file outputs and generate their
    contents as strings.  Kept for backward compatibility with
    ``DataSchemaStep``, ``StreamUtilsStep``, and ``ArrayUtilsStep``.

BuildDag
    A directed acyclic graph of ``BuildStep`` nodes.  Steps are added via
    ``add()``; ``run()`` executes them in dependency order, injecting the
    right artifacts and params automatically.
"""
from __future__ import annotations

import time
import traceback
from abc import ABC, abstractmethod
from collections import deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar


# ---------------------------------------------------------------------------
# BuildConfig
# ---------------------------------------------------------------------------

@dataclass
class BuildConfig:
    """
    Configuration for a Waveflow build.

    Parameters
    ----------
    root_dir : Path | str | None
        Project root directory.  All relative output paths are resolved
        against this.  Defaults to the current working directory.
    vitis_version : str | None
        Vitis HLS version in ``"YYYY.M"`` format (e.g. ``"2025.1"``).
        Controls which compatibility files are emitted.  ``None`` means
        conservative / legacy behaviour.
    platform : str | None
        Name of the calibration platform (a directory under ``platforms_root``).  When set, it is
        resolved at construction into :attr:`platform_info` — created (seeded with ``part`` /
        ``clk_freq``) if absent, or **confirmed** against the stored manifest if present.  This is the
        single source both the synthesis TCL and the calibration load/store read, so the synthesised
        part cannot drift from the calibrated one.  ``None`` = no platform (uncalibrated: components
        degrade to the plain per-word timing).
    part : str | None
        FPGA part (the ``set_part`` string, e.g. ``"xc7z020clg484-1"``) this build targets.
    clk_freq : float | None
        Synthesis clock frequency in Hz (its period drives HLS scheduling → the cycle counts).
    platforms_root : Path | str | None
        The build's **primary** platform library — the write target, and the first root searched.
        Defaults to ``calib/platforms`` (a project-local library).  When a selected platform is not
        found there, resolution falls back (read-only) through
        :func:`~waveflow.calib.platform.platform_fallback_path` — the ``WAVEFLOW_PLATFORM_PATH`` env,
        the per-user library, and the reference platforms shipped inside the package — so a
        ``pip``-installed build resolves ``zynq7020_bfm_100mhz`` with no checkout.
    allow_platform_mismatch : bool
        When the selected platform's stored part/clock differ from this build's, warn instead of
        raising (default ``False`` = raise).
    """

    root_dir: Path | str | None = None
    vitis_version: str | None = None
    params: dict = field(default_factory=dict)
    platform: str | None = None
    part: str | None = None
    clk_freq: float | None = None
    platforms_root: Path | str | None = None
    allow_platform_mismatch: bool = False

    def __post_init__(self) -> None:
        self.root_dir = Path.cwd() if self.root_dir is None else Path(self.root_dir)
        self.platforms_root = (
            Path("calib/platforms") if self.platforms_root is None else Path(self.platforms_root))
        #: The resolved :class:`~waveflow.calib.platform.Platform` (created-or-confirmed), or ``None``
        #: when no ``platform`` was selected.
        self.platform_info = None
        if self.platform is not None:
            from waveflow.calib.platform import Platform, platform_fallback_path
            self.platform_info = Platform.resolve(
                self.platforms_root, self.platform, part=self.part, clk_freq=self.clk_freq,
                allow_mismatch=self.allow_platform_mismatch, fallbacks=platform_fallback_path())

    def vitis_version_tuple(self) -> tuple[int, int] | None:
        """Parse ``vitis_version`` into a ``(major, minor)`` integer tuple.

        Returns ``None`` when ``vitis_version`` is ``None``.

        Raises ``ValueError`` when the format is not ``"YYYY.M"``.
        """
        if self.vitis_version is None:
            return None
        parts = self.vitis_version.split(".")
        if len(parts) != 2:
            raise ValueError(
                f"Invalid vitis_version '{self.vitis_version}'. Expected format 'YYYY.M'."
            )
        try:
            return int(parts[0]), int(parts[1])
        except ValueError:
            raise ValueError(
                f"Invalid vitis_version '{self.vitis_version}'. Expected format 'YYYY.M'."
            )

    def needs_legacy_streamutils_cpp(self) -> bool:
        """Return ``True`` when ``streamutils.cpp`` must be included.

        Required for Vitis versions strictly older than ``2025.1``.  When no
        version is specified the conservative default is to include it.
        """
        ver = self.vitis_version_tuple()
        return True if ver is None else ver < (2025, 1)


# ---------------------------------------------------------------------------
# BuildArtifact hierarchy  (kept for Buildable backward compatibility)
# ---------------------------------------------------------------------------

class BuildArtifact(ABC):
    """Base for any value produced by a BuildStep."""

    timestamp: float

    @abstractmethod
    def is_fresh_relative_to(self, other: BuildArtifact) -> bool:
        """True if this artifact is newer than *other*."""
        ...


@dataclass(eq=False)
class FileArtifact(BuildArtifact):
    """A file written to disk.  Freshness is checked via the file's mtime."""

    path: Path
    timestamp: float = field(default_factory=time.time)

    def is_fresh_relative_to(self, other: BuildArtifact) -> bool:
        return self.path.stat().st_mtime > other.timestamp

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Path):
            return self.path == other
        if isinstance(other, FileArtifact):
            return self.path == other.path
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self.path)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.path, name)


@dataclass
class ObjectArtifact(BuildArtifact):
    """An in-memory Python object (sim result, DataFrame, parsed report, …)."""

    value: Any
    timestamp: float = field(default_factory=time.time)

    def is_fresh_relative_to(self, other: BuildArtifact) -> bool:
        return self.timestamp > other.timestamp


def source_artifact(path: Path) -> FileArtifact:
    """Wrap a source file as a FileArtifact using its mtime as the timestamp."""
    return FileArtifact(path=path, timestamp=path.stat().st_mtime)


def _path_mtime(p: Path) -> float | None:
    """Return effective mtime for *p*, or ``None`` if missing/empty.

    For files: the file's mtime.  For directories: the max mtime of any
    file inside, recursively.  Returns ``None`` if the path is missing or
    if a directory contains no files.  Directories on Windows (and most
    POSIX systems) don't update their own mtime when files inside change,
    so we use the contained-file mtime instead.
    """
    if not p.exists():
        return None
    if p.is_dir():
        mtimes = [c.stat().st_mtime for c in p.rglob("*") if c.is_file()]
        return max(mtimes) if mtimes else None
    return p.stat().st_mtime


# ---------------------------------------------------------------------------
# BuildResult
# ---------------------------------------------------------------------------

@dataclass
class BuildResult:
    """
    Outcome of a single :class:`BuildStep` execution.

    Parameters
    ----------
    success : bool
        ``True`` if the step completed without errors.
    message : str
        Human-readable status or error description.
    artifacts : dict[str, Any]
        Mapping of output name → artifact value (raw Python objects or Paths).
    timestamp : float
        Wall-clock time when this result was created.
    elapsed_seconds : float
        How long the step took to run, in wall-clock seconds.  ``0.0`` for a skipped step (it did no
        work).  Recorded on every step because it is the raw material for answering "what would
        recalibrating here cost?" — a question better answered from a history of real runs than from
        an estimate, and unanswerable after the fact if it was never measured.
    traceback : str | None
        Formatted traceback of the exception that failed the step, or ``None`` when the step
        succeeded or failed without raising.  ``message`` alone is ``str(exc)``, which for a
        toolchain error is often a bare string with no file, line or call site — enough to know
        something broke and not enough to find it.  Keeping the traceback here means a caller can
        surface it without every step having to catch and log for itself.
    """

    success: bool
    message: str = ""
    artifacts: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)
    skipped: bool = False
    elapsed_seconds: float = 0.0
    traceback: str | None = None

    def object(self, name: str) -> Any:
        """Return the artifact value for *name*."""
        return self.artifacts[name]

    def path(self, name: str) -> Path:
        """Return the artifact value for *name* as a Path."""
        return self.artifacts[name]


# ---------------------------------------------------------------------------
# BuildStep
# ---------------------------------------------------------------------------

@dataclass(kw_only=True)
class BuildStep(ABC):
    """
    Abstract base for any node in a build DAG.

    Subclasses declare ``description``, ``consumes``, ``produces``, and
    ``params`` as plain class attributes (no annotation) which shadow these
    ``ClassVar`` defaults.  The DAG reads them to wire dependencies and inject
    the right values into ``run()``.

    ``run()`` receives consumed artifact values and resolved config params as
    keyword arguments and must return a dict mapping each name in ``produces``
    to its produced value.  Raise ``RuntimeError`` to signal failure.
    """

    name: str = ""
    description: str = ""
    consumes: ClassVar[list] = []
    produces: ClassVar[dict] = {}
    params: ClassVar[dict] = {}

    def __post_init__(self) -> None:
        if not self.name:
            self.name = self._default_name()
        if not self.description:
            self.description = self._default_description()

    def _default_name(self) -> str:
        """Default step name when ``name`` is not explicitly provided."""
        return type(self).__name__

    def _default_description(self) -> str:
        """Look up a class-level ``description = "..."`` attribute via MRO.

        Subclasses may declare a class-level default like ``description = "..."``
        without annotation; this is picked up when no per-instance value is
        provided.
        """
        for cls in type(self).__mro__:
            if cls is BuildStep:
                break
            d = cls.__dict__.get("description")
            if isinstance(d, str) and d:
                return d
        return ""

    def expected_paths(self, config: BuildConfig) -> dict[str, Path]:
        """Override to declare file paths that depend on config.params at run time.

        The DAG uses ``produces`` values for static path declarations; this
        method is only needed when the path cannot be expressed as a plain
        ``Path`` in ``produces`` (e.g. it is determined by a param value).
        """
        return {}

    @abstractmethod
    def run(self, config: BuildConfig, **kwargs) -> dict[str, Any]:
        """Execute the step.

        ``kwargs`` contains consumed artifact values and resolved config params
        injected by the DAG.  Return a dict mapping artifact names (must match
        ``self.produces``) to their values.  Raise ``RuntimeError`` on failure.
        """
        ...


# ---------------------------------------------------------------------------
# SourceStep
# ---------------------------------------------------------------------------

@dataclass(kw_only=True)
class SourceStep(BuildStep):
    """A source file that serves as a root dependency in the build graph.

    Unlike regular BuildSteps whose produces is a fixed class attribute,
    SourceStep.produces is instance-level because the artifact name varies
    per source file.  path may be absolute or relative to config.root_dir.
    """

    description = "A source file dependency."
    consumes = []
    params = {}

    artifact: str
    path: Path

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        super().__post_init__()

    def _default_name(self) -> str:
        return self.artifact

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {self.artifact: self.path}

    def _resolved(self, config: BuildConfig) -> Path:
        return self.path if self.path.is_absolute() else config.root_dir / self.path

    def expected_paths(self, config: BuildConfig) -> dict[str, Path]:
        return {self.artifact: self._resolved(config)}

    def run(self, config: BuildConfig, **_) -> dict[str, Any]:
        p = self._resolved(config)
        if not p.exists():
            raise RuntimeError(f"Source file not found: {p}")
        return {self.artifact: p}


# ---------------------------------------------------------------------------
# Buildable  (legacy — kept for DataSchemaStep / StreamUtilsStep / ArrayUtilsStep)
# ---------------------------------------------------------------------------

class Buildable(BuildStep):
    """
    A :class:`BuildStep` that declares named file outputs and generates their
    content.

    Subclasses implement:

    * :attr:`build_outputs` — ``dict[str, Path]`` mapping output name to a
      path **relative to** ``config.root_dir``.
    * :meth:`generate` — return the file content for a given output key as a
      string.

    The default :meth:`run` implementation iterates over all declared outputs,
    calls :meth:`generate` for each, and writes the results to disk.
    """

    deps: ClassVar[list] = []

    @property
    @abstractmethod
    def build_outputs(self) -> dict[str, Path]:
        """Mapping of output name → path relative to ``config.root_dir``."""
        ...

    @abstractmethod
    def generate(self, key: str, config: BuildConfig) -> str:
        """Return the generated file content for *key* as a string."""
        ...

    def is_fresh(self, config: BuildConfig, results: dict[str, BuildResult]) -> bool:
        """True when all output files exist and are newer than every dep FileArtifact."""
        for rel_path in self.build_outputs.values():
            out_path = config.root_dir / rel_path
            if not out_path.exists():
                return False
            out_mtime = out_path.stat().st_mtime
            for dep in getattr(self, 'deps', []):
                dep_result = results.get(dep.name)
                if dep_result is None:
                    return False
                for artifact in dep_result.artifacts.values():
                    if isinstance(artifact, FileArtifact):
                        if out_mtime <= artifact.timestamp:
                            return False
        return True

    def run(self, config: BuildConfig, results: dict[str, BuildResult] = {}) -> BuildResult:
        artifacts: dict[str, Any] = {}
        errors: list[str] = []
        for key, rel_path in self.build_outputs.items():
            try:
                content = self.generate(key, config)
                out_path = config.root_dir / rel_path
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(content, encoding="utf-8")
                artifacts[key] = FileArtifact(path=out_path)
            except Exception as exc:
                errors.append(f"{key}: {exc}")
        if errors:
            return BuildResult(success=False, message="; ".join(errors), artifacts=artifacts)
        return BuildResult(success=True, artifacts=artifacts)


# ---------------------------------------------------------------------------
# BuildDag
# ---------------------------------------------------------------------------

class BuildDag:
    """
    A directed acyclic graph of :class:`BuildStep` nodes.

    Steps are added via :meth:`add`.  Dependencies are derived automatically
    from each step's ``consumes`` and ``produces`` class attributes.  Legacy
    :class:`Buildable` steps that override ``resolve_deps`` are also supported.

    :meth:`run` executes the steps in topological order, injecting consumed
    artifacts and resolved params into each step's ``run()`` call.
    """

    def __init__(self) -> None:
        self._steps: list[BuildStep] = []
        self._names: set[str] = set()
        self._artifact_owners: dict[str, str] = {}
        self._artifact_paths: dict[str, Path | None] = {}
        self._step_by_name: dict[str, BuildStep] = {}

    def add(self, step: BuildStep) -> BuildStep:
        """Register *step*, auto-derive its deps, and return it."""
        # Defensive: legacy Buildable subclasses may bypass dataclass __post_init__
        if not getattr(step, "name", ""):
            step.name = type(step).__name__
        if step.name in self._names:
            raise ValueError(
                f"A step named '{step.name}' already exists in this BuildDag."
            )

        for name, path_spec in step.produces.items():
            if name in self._artifact_owners:
                raise ValueError(
                    f"Artifact '{name}' already claimed by '{self._artifact_owners[name]}', "
                    f"cannot also be produced by '{step.name}'."
                )
            self._artifact_owners[name] = step.name
            if isinstance(path_spec, str):
                if path_spec not in self._artifact_paths:
                    raise ValueError(
                        f"Step '{step.name}' artifact '{name}' references unknown artifact "
                        f"'{path_spec}' — add that step first."
                    )
                self._artifact_paths[name] = self._artifact_paths[path_spec]
            else:
                self._artifact_paths[name] = path_spec  # Path or None

        step_deps: list[BuildStep] = []
        for name in step.consumes:
            if name not in self._artifact_owners:
                raise ValueError(
                    f"Step '{step.name}' consumes '{name}' but no registered step produces it."
                )
            dep_name = self._artifact_owners[name]
            if dep_name not in {d.name for d in step_deps}:
                step_deps.append(self._step_by_name[dep_name])

        if hasattr(step, 'resolve_deps'):
            step.resolve_deps(self._steps)
        for dep in getattr(step, 'deps', []):
            if dep.name not in {d.name for d in step_deps}:
                step_deps.append(dep)
        for dep in getattr(step, '_deps', []):
            if dep.name not in {d.name for d in step_deps}:
                step_deps.append(dep)

        step._deps = step_deps
        self._steps.append(step)
        self._names.add(step.name)
        self._step_by_name[step.name] = step
        return step

    def step_names(self) -> list[str]:
        """Return step names in topological execution order."""
        return [s.name for s in self._topological_sort()]

    def steps(self) -> list[BuildStep]:
        """Return steps in topological execution order."""
        return list(self._topological_sort())

    def artifact_owners(self) -> dict[str, str]:
        """Return mapping of artifact name → producing step name, in add() order."""
        return dict(self._artifact_owners)

    def run(
        self,
        config: BuildConfig,
        through: str | None = None,
        force: bool | Iterable[str] = False,
        on_step_begin: Callable[[BuildStep, bool, dict[str, Path]], None] | None = None,
        on_step_end: Callable[[BuildStep, BuildResult], None] | None = None,
    ) -> dict[str, BuildResult]:
        """Run all steps in dependency order.

        Parameters
        ----------
        config : BuildConfig
        through : str | None
            When set, only execute the named step and its transitive
            dependencies.  Raises ``ValueError`` if no step has that name.
        force : bool | Iterable[str]
            When ``False`` (default), steps whose produced files are newer
            than their consumed files are skipped.  When ``True``, every
            step runs.  When an iterable of step names, only those steps
            are forced to run (downstream steps re-run via cascade when
            their inputs become newer).
        on_step_begin, on_step_end : callable, optional
            Progress callbacks.  ``on_step_begin(step, will_run, paths)``
            fires before each step; ``will_run`` is True if the step will
            actually execute (False = skipped by the freshness check), and
            ``paths`` maps each file-artifact name to its resolved path.
            ``on_step_end(step, result)`` fires after, with the BuildResult.
            Note that once a step fails the build halts, so neither callback
            fires for the steps after it — they are never reached, and get no
            entry in the returned mapping at all.

        Returns a mapping of step name → :class:`BuildResult`.
        """
        order = self._topological_sort()

        if through is not None:
            by_name = {s.name: s for s in order}
            if through not in by_name:
                raise ValueError(
                    f"No step named '{through}' in this BuildDag. "
                    f"Available: {list(by_name)}"
                )
            included: set[str] = set()

            def _collect(name: str) -> None:
                if name in included:
                    return
                included.add(name)
                for dep in by_name[name]._deps:
                    _collect(dep.name)

            _collect(through)
            order = [s for s in order if s.name in included]

        if force is True:
            forced: set[str] = {s.name for s in order}
        elif force is False:
            forced = set()
        else:
            forced = set(force)
            unknown = forced - {s.name for s in self._steps}
            if unknown:
                raise ValueError(f"Unknown step name(s) in force: {sorted(unknown)}")

        all_paths = self.artifact_paths(config)
        must_run = self._determine_must_run(order, all_paths, forced)
        artifact_store: dict[str, Any] = {}
        results: dict[str, BuildResult] = {}
        failed: set[str] = set()

        for step in order:
            if failed:
                # A previous step failed — halt the build.
                break

            will_run = step.name in must_run
            step_paths = {n: all_paths[n] for n in step.produces if n in all_paths}
            if on_step_begin is not None:
                on_step_begin(step, will_run, step_paths)

            if step.name not in must_run:
                skip_artifacts: dict[str, Any] = {}
                for name in step.produces:
                    p = all_paths.get(name)
                    if p is not None:
                        skip_artifacts[name] = p
                        artifact_store[name] = p
                results[step.name] = BuildResult(
                    success=True, skipped=True, artifacts=skip_artifacts
                )
            else:
                # Wall-clock is recorded for every step, success or failure.  It is the raw fact
                # behind "what does a calibration point cost?" — an answer better derived from a
                # history of actual runs than from any estimate, and one nothing can reconstruct
                # after the fact if it was never measured.
                started = time.perf_counter()
                try:
                    produced = self._call_run(step, config, artifact_store)
                    results[step.name] = BuildResult(
                        success=True, artifacts=produced,
                        elapsed_seconds=time.perf_counter() - started)
                except Exception as exc:
                    # Keep the traceback, not just str(exc).  A toolchain failure often stringifies
                    # to a bare message with no file, line or call site; without this the caller has
                    # no way to recover where it came from.
                    results[step.name] = BuildResult(
                        success=False, message=str(exc),
                        traceback=traceback.format_exc(),
                        elapsed_seconds=time.perf_counter() - started)
                    failed.add(step.name)

            if on_step_end is not None:
                on_step_end(step, results[step.name])

        return results

    def _determine_must_run(
        self,
        order: list[BuildStep],
        all_paths: dict[str, Path],
        forced: set[str],
    ) -> set[str]:
        """Compute the set of step names that must actually run; the rest skip.

        A step must run if any of:
        1. It is in *forced*.
        2. It is a legacy ``Buildable`` (doesn't participate in skip logic).
        3. Any of its produced files are missing or older than a consumed file.
        4. *(Cascade)* Any of its direct upstream dependencies must run.
        5. *(In-memory demand)* It produces an in-memory artifact that is
           consumed by a step that must run.

        Rules 4 and 5 are propagated to a fixed point so the answer is stable.
        """
        must_run: set[str] = set()

        # Phase 1: direct conditions (1), (2), (3)
        for step in order:
            if step.name in forced or isinstance(step, Buildable):
                must_run.add(step.name)
            elif self._files_stale(step, all_paths):
                must_run.add(step.name)

        # Phase 2: propagate cascade (4) and in-memory demand (5) until stable
        in_order = {s.name for s in order}
        changed = True
        while changed:
            changed = False
            for step in order:
                if step.name in must_run:
                    continue
                # (4) Cascade: if any dep that's part of this run must run, so do we
                if any(d.name in must_run for d in step._deps if d.name in in_order):
                    must_run.add(step.name)
                    changed = True
                    continue
                # (5) In-memory demand: if a must_run step consumes one of our
                # in-memory outputs, we need to produce it
                in_mem_outputs = {
                    n for n in step.produces if self._artifact_paths.get(n) is None
                }
                if not in_mem_outputs:
                    continue
                for downstream in order:
                    if downstream.name in must_run and in_mem_outputs & set(downstream.consumes):
                        must_run.add(step.name)
                        changed = True
                        break

        return must_run

    def _files_stale(self, step: BuildStep, all_paths: dict[str, Path]) -> bool:
        """True if step's produced files are missing or older than its consumed files.

        Steps with no file outputs return ``False`` here; they are handled by
        in-memory demand propagation instead.
        """
        if isinstance(step, Buildable):
            return False

        file_outputs = [n for n in step.produces if self._artifact_paths.get(n) is not None]
        if not file_outputs:
            return False

        produced_mtimes: list[float] = []
        for name in file_outputs:
            m = _path_mtime(all_paths[name])
            if m is None:
                return True
            produced_mtimes.append(m)

        min_produced = min(produced_mtimes)
        for consumed in step.consumes:
            cp = all_paths.get(consumed)
            if cp is None:
                continue
            cm = _path_mtime(cp)
            if cm is None or cm > min_produced:
                return True
        return False

    @staticmethod
    def _call_run(
        step: BuildStep,
        config: BuildConfig,
        artifact_store: dict[str, Any],
    ) -> dict[str, Any]:
        if isinstance(step, Buildable):
            result = step.run(config)
            if not result.success:
                raise RuntimeError(result.message or "Step failed")
            return result.artifacts

        missing = [n for n in step.consumes if n not in artifact_store]
        if missing:
            raise RuntimeError(f"Step '{step.name}': missing consumed artifacts: {missing}")
        inputs = {n: artifact_store[n] for n in step.consumes}
        resolved_params = {
            n: config.params.get(n, default)
            for n, default in step.params.items()
        }
        produced = step.run(config, **inputs, **resolved_params)
        for key in step.produces:
            if key not in produced:
                raise RuntimeError(
                    f"Step '{step.name}' declared '{key}' in produces but did not return it."
                )
        artifact_store.update(produced)
        return produced

    def _topological_sort(self) -> list[BuildStep]:
        step_by_name = {s.name: s for s in self._steps}
        in_degree: dict[str, int] = {s.name: 0 for s in self._steps}
        adj: dict[str, list[str]] = {s.name: [] for s in self._steps}

        for step in self._steps:
            for dep in step._deps:
                if dep.name not in in_degree:
                    raise ValueError(
                        f"Dependency '{dep.name}' of step '{step.name}' "
                        "is not registered in this BuildDag."
                    )
                adj[dep.name].append(step.name)
                in_degree[step.name] += 1

        queue: deque[str] = deque(
            name for name, deg in in_degree.items() if deg == 0
        )
        order: list[BuildStep] = []
        while queue:
            name = queue.popleft()
            order.append(step_by_name[name])
            for dependent in adj[name]:
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)

        if len(order) != len(self._steps):
            raise ValueError("BuildDag contains a cycle.")
        return order

    def artifact_paths(self, config: BuildConfig) -> dict[str, Path]:
        """Return a mapping of every file artifact name to its resolved path.

        Relative paths (declared in ``produces``) are resolved against
        ``config.root_dir``.  Absolute paths are used as-is.  Steps may
        override ``expected_paths()`` to supply paths that depend on
        ``config.params``; those take precedence over the static declaration.
        In-memory artifacts (declared as ``None``) are omitted.
        """
        result: dict[str, Path] = {}
        for step in self._topological_sort():
            for name in step.produces:
                static = self._artifact_paths.get(name)
                if static is not None:
                    result[name] = static if static.is_absolute() else config.root_dir / static
            # Dynamic override (e.g. path determined by a config.param value)
            result.update(step.expected_paths(config))
        return result

    def results_status(self, config: BuildConfig) -> list[dict]:
        """Pre-build freshness status for every file artifact in the DAG.

        Resolves all file artifact paths (from ``produces`` declarations and
        ``expected_paths()`` overrides), then checks existence, mtime, and
        staleness relative to consumed file artifacts.

        Returns a list of dicts, one per file artifact:
        ``artifact``, ``produced_by``, ``path``, ``exists``, ``mtime``
        (float | None), ``stale`` (bool), ``stale_because`` (list[str]).
        """
        all_paths = self.artifact_paths(config)
        order = self._topological_sort()
        file_status: dict[str, dict] = {}
        entries: list[dict] = []

        for step in order:
            for artifact_name in step.produces:
                path = all_paths.get(artifact_name)
                if path is None:
                    continue

                consumed_file_artifacts = [
                    name for name in step.consumes if name in file_status
                ]

                mtime = _path_mtime(path)
                exists = mtime is not None
                stale_because: list[str] = []

                if not exists:
                    stale = True
                else:
                    for dep_name in consumed_file_artifacts:
                        dep = file_status[dep_name]
                        if dep["stale"]:
                            stale_because.append(dep_name)
                        elif dep["mtime"] is not None and dep["mtime"] > mtime:
                            stale_because.append(dep_name)
                    stale = bool(stale_because)

                entry = {
                    "artifact": artifact_name,
                    "produced_by": step.name,
                    "path": path,
                    "exists": exists,
                    "mtime": mtime,
                    "stale": stale,
                    "stale_because": stale_because,
                }
                file_status[artifact_name] = entry
                entries.append(entry)

        return entries

    def info(self) -> list[dict]:
        """Return structured information about every step in the DAG.

        Each entry is a ``dict`` with keys:
        ``step``, ``description``, ``consumes``, ``produces``, ``params``.
        """
        result = []
        for step in self._steps:
            result.append({
                "step": step.name,
                "description": step.description,
                "consumes": list(step.consumes),
                "produces": list(step.produces),
                "params": dict(step.params),
            })
        return result

    def describe(self) -> str:
        """Return a markdown table summarising all steps."""
        header = "| Step | Description | Consumes | Produces | Params |"
        sep    = "|---|---|---|---|---|"
        lines  = [header, sep]
        for row in self.info():
            step     = row["step"]
            desc     = row["description"] or "—"
            consumes = ", ".join(row["consumes"]) if row["consumes"] else "—"
            produces = ", ".join(row["produces"]) if row["produces"] else "—"
            params   = ", ".join(row["params"])   if row["params"]   else "—"
            lines.append(f"| {step} | {desc} | {consumes} | {produces} | {params} |")
        return "\n".join(lines)
