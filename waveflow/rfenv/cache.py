"""cache.py -- trace a scene once, keep its channel file, and reuse it while it still describes the scene.

A ray trace is the one expensive, Sionna-only step in the RF environment; everything after it needs
a channel file and numpy.  :func:`load_or_trace` returns the channel for a
:class:`~waveflow.rfenv.sionna_backend.SceneSpec`, from the file when the file describes that
specification, and by tracing (then saving) otherwise.  :class:`TraceChannelStep` is the same thing
as a build-DAG step, for examples that commit their channel files.

**When a file still describes the scene.**  Its manifest's ``spec_hash`` equals the specification's
(the scene -- an XML file by its bytes --, the carrier, the devices, the arrays, the solver
settings), **and**, when Sionna is installed here, the versions it was traced with
(``generator.versions``: sionna-rt, Mitsuba, Dr.Jit) equal the installed ones.  A version change
retraces, because Sionna's numbers move between releases.  Without Sionna a matching file is used as
it is -- that is the point of committing it -- and the variant (CPU or GPU) is not a reason to
retrace: it changes float32 rounding, not the channel.

``retrace`` overrides the rule: ``"never"`` uses any file whose hash matches (a committed reference
that must not be rewritten by whoever runs the tests), ``"always"`` traces regardless.
"""

from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from waveflow.build.build import BuildConfig, BuildStep
from waveflow.rfenv.paths import PathSet, file_names
from waveflow.rfenv.sionna_backend import SceneSpec

#: The packages whose versions decide whether a traced file is stale.
TRACER_PACKAGES = ("sionna-rt", "mitsuba", "drjit")
#: ``retrace`` policies.
RETRACE = ("if_stale", "never", "always")


def installed_tracer_versions() -> dict[str, str] | None:
    """The installed tracer versions, or ``None`` when Sionna RT is not installed (no import)."""
    out = {}
    for pkg in TRACER_PACKAGES:
        try:
            out[pkg] = importlib.metadata.version(pkg)
        except importlib.metadata.PackageNotFoundError:
            return None
    return out


def describes(pathset: PathSet, spec: SceneSpec) -> bool:
    """Whether a channel file is the trace of ``spec`` on this install (see the module doc)."""
    if pathset.manifest.get("spec_hash") != spec.hash():
        return False
    here = installed_tracer_versions()
    if here is None:
        return True
    traced = pathset.manifest.get("generator", {}).get("versions", {})
    return all(traced.get(pkg) == v for pkg, v in here.items())


def load_or_trace(
    spec: SceneSpec, stem: str | Path, *, retrace: str = "if_stale"
) -> PathSet:
    """The channel of ``spec``: from ``<stem>.npz``/``.json`` when it still describes the scene,
    otherwise traced with Sionna RT and saved there."""
    if retrace not in RETRACE:
        raise ValueError(f"retrace must be one of {RETRACE}, got {retrace!r}")
    npz, js = file_names(stem)
    if retrace == "never":
        ps = PathSet.load(stem)  # a missing part is a FileNotFoundError, never a trace
        if ps.manifest["spec_hash"] != spec.hash():
            raise ValueError(
                f"{js} is not the trace of this specification (its spec_hash differs), and "
                f"retrace='never' forbids tracing it again"
            )
        return ps
    if retrace == "if_stale" and npz.is_file() and js.is_file():
        cached: PathSet | None
        try:
            cached = PathSet.load(stem)
        except ValueError:
            # written by another schema version, or damaged: trace it again
            cached = None
        if cached is not None and describes(cached, spec):
            return cached
    from waveflow.rfenv.sionna_backend import trace

    ps = trace(spec)
    ps.save(stem)
    return ps


@dataclass(kw_only=True)
class TraceChannelStep(BuildStep):
    """A build step that keeps a scene's channel file current: :func:`load_or_trace` in a DAG.

    ``stem`` is relative to the build's ``root_dir`` (or absolute).  The step produces the channel
    file's manifest under ``artifact``; it is fresh -- skipped -- while the file still describes
    ``spec`` (:func:`describes`), whatever the files' mtimes say.
    """

    description = "Trace a scene with Sionna RT into a channel file, or keep the one that is current."
    consumes: ClassVar[list] = []
    params: ClassVar[dict] = {}

    artifact: str = "channel"
    spec: SceneSpec
    stem: Path
    retrace: str = "if_stale"

    def __post_init__(self) -> None:
        self.stem = Path(self.stem)
        super().__post_init__()

    @property
    def produces(self) -> dict:  # type: ignore[override]
        return {self.artifact: file_names(self.stem)[1]}

    def _stem(self, config: BuildConfig) -> Path:
        return (
            self.stem
            if self.stem.is_absolute()
            else Path(config.root_dir or ".") / self.stem
        )

    def expected_paths(self, config: BuildConfig) -> dict[str, Path]:
        return {self.artifact: file_names(self._stem(config))[1]}

    def is_fresh(self, config: BuildConfig, paths: dict[str, Path]) -> bool | None:
        npz, js = file_names(self._stem(config))
        if self.retrace == "always" or not (npz.is_file() and js.is_file()):
            return False
        try:
            ps = PathSet.load(self._stem(config))
        except ValueError:
            return False
        if self.retrace == "never":  # the hash decides; versions do not
            return ps.manifest["spec_hash"] == self.spec.hash()
        return describes(ps, self.spec)

    def run(self, config: BuildConfig, **_: Any) -> dict[str, Any]:
        load_or_trace(self.spec, self._stem(config), retrace=self.retrace)
        return {self.artifact: file_names(self._stem(config))[1]}
