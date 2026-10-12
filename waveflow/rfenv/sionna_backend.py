"""sionna_backend.py -- a scene traced by NVIDIA Sionna RT into a :class:`~waveflow.rfenv.paths.PathSet`.

**The only module that imports Sionna**, and only inside :func:`trace`: the rest of
:mod:`waveflow.rfenv` -- applying a channel, the nodes in cached mode -- runs on a channel file and
numpy alone, so the ``sionna`` extra stays optional (``plans/sionna_rt/sionna_rt.md`` §2).

**A scene specification is data.**  :class:`SceneSpec` names the scene (``"empty"``, a built-in such
as ``"floor_wall"``, or a Mitsuba XML file), the carrier, the transmitters and receivers with their
positions, orientations and velocities, the two arrays, and the solver settings.  It converts to
canonical JSON, and its hash keys the cache: the same specification traced on the same install gives
the same channel file (``PathSolver(deterministic=True)``).  For an XML scene the hash covers the
file's bytes, not only its name.

**What is kept from Sionna**, and why (the step 2 spike, ``plans/sionna_rt/spike/``):

* ``a`` comes from ``Paths.cir(normalize_delays=False, out_type="numpy")``, never ``Paths.a``:
  ``cir()`` multiplies in the carrier phase e^(-j2pi f tau), and ``Paths.a`` lacks it.  With
  ``normalize_delays=True`` (Sionna's default) the delays *and the phases* would be re-referenced
  to the first path.
* With the synthetic array (the default) ``tau``, ``doppler`` and ``valid`` come per device pair,
  ``[rx, tx, p]``, and the per-antenna phase sits in ``a``; they are broadcast across the antennas.
  Without it they come per antenna pair.  Either way the result is flattened to
  ``[rx * rx_ant, tx * tx_ant, p]``: one channel per (device, antenna), named in the manifest.
* Unused path slots are padded (``tau = -1``, ``a = 0``) and marked invalid.
* A dual-polarized array (``"VH"``, ``"cross"``) has two elements per position, and Sionna orders a
  device's antennas **pattern-major**: the first ``num_rows * num_cols`` channels are the first
  polarization, the next as many the second.  The channel map's ``antenna`` index is that order.
* Sionna computes in single precision: about 3.6e-4 rad of carrier phase at 100 m and 3.5 GHz.  The
  arrays are widened to float64/complex128 for application; the precision is still float32's.

**The variant.**  ``import sionna.rt`` keeps a Mitsuba variant set before it and otherwise picks one
(CUDA when present, else ``llvm_ad_mono_polarized``).  ``WAVEFLOW_SIONNA_VARIANT`` sets one
explicitly before the import.  The manifest records the variant and every version, because a channel
traced on a GPU host is not bit-identical to one traced here.
"""

from __future__ import annotations

import datetime
import hashlib
import importlib.metadata
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from waveflow.rfenv.paths import (
    ABSOLUTE_DELAYS,
    SCHEMA_VERSION,
    PathSet,
    channel_map,
    spec_hash,
)

#: Environment variable naming a Mitsuba variant to set before ``import sionna.rt``.
VARIANT_ENV = "WAVEFLOW_SIONNA_VARIANT"
#: What to tell someone who called the backend without the extra.
INSTALL_HINT = (
    'Tracing a scene needs Sionna RT: pip install -e ".[sionna]" '
    "(a committed channel file needs only numpy; see waveflow.rfenv)"
)
#: The packages whose versions a traced channel file records.
VERSIONED = ("sionna-rt", "mitsuba", "drjit", "numpy")

Vec3 = tuple[float, float, float]


@dataclass(frozen=True)
class DeviceSpec:
    """A transmitter or receiver: where it is, which way it faces, how it moves.

    ``orientation`` is (yaw, pitch, roll) in radians; ``look_at`` a point it faces instead.  At most
    one of the two.  ``velocity`` in m/s gives the paths their Doppler shifts.
    """

    name: str
    position: Vec3
    orientation: Vec3 | None = None
    look_at: Vec3 | None = None
    velocity: Vec3 = (0.0, 0.0, 0.0)

    def __post_init__(self) -> None:
        if self.orientation is not None and self.look_at is not None:
            raise ValueError(
                f"device {self.name!r}: give orientation or look_at, not both"
            )
        # plain float triples: Mitsuba's Point3f refuses numpy scalars, and JSON needs plain numbers
        for name in ("position", "orientation", "look_at", "velocity"):
            v = getattr(self, name)
            if v is not None:
                if len(v) != 3:
                    raise ValueError(
                        f"device {self.name!r}: {name} needs 3 values, got {v!r}"
                    )
                object.__setattr__(self, name, tuple(float(x) for x in v))


@dataclass(frozen=True)
class ArraySpec:
    """A planar antenna array (Sionna's ``PlanarArray``); spacings in wavelengths."""

    num_rows: int = 1
    num_cols: int = 1
    vertical_spacing: float = 0.5
    horizontal_spacing: float = 0.5
    pattern: str = "iso"
    polarization: str = "V"


@dataclass(frozen=True)
class SolverSpec:
    """``PathSolver`` settings (Sionna RT 2.2.0 names and defaults, ``deterministic`` always on)."""

    max_depth: int = 3
    max_num_paths_per_src: int = 1_000_000
    samples_per_src: int = 1_000_000
    synthetic_array: bool = True
    los: bool = True
    specular_reflection: bool = True
    diffuse_reflection: bool = False
    refraction: bool = True
    diffraction: bool = False
    edge_diffraction: bool = False
    diffraction_lit_region: bool = True
    seed: int = 42


@dataclass(frozen=True)
class SceneSpec:
    """Everything a trace depends on: the scene, the carrier, the devices, the arrays, the solver."""

    scene: str
    frequency_hz: float
    transmitters: tuple[DeviceSpec, ...]
    receivers: tuple[DeviceSpec, ...]
    tx_array: ArraySpec = field(default_factory=ArraySpec)
    rx_array: ArraySpec = field(default_factory=ArraySpec)
    solver: SolverSpec = field(default_factory=SolverSpec)

    def __post_init__(self) -> None:
        if not self.transmitters or not self.receivers:
            raise ValueError(
                "a scene specification needs at least one transmitter and receiver"
            )
        if not float(self.frequency_hz) > 0:
            raise ValueError(f"frequency_hz must be positive, got {self.frequency_hz}")
        object.__setattr__(
            self, "frequency_hz", float(self.frequency_hz)
        )  # 3.5e9 == 3_500_000_000
        names = [d.name for d in (*self.transmitters, *self.receivers)]
        if len(set(names)) != len(names):
            raise ValueError(f"device names must be unique, got {names}")

    def to_dict(self) -> dict[str, Any]:
        """Canonical, JSON-ready form: plain lists and numbers (what :meth:`from_dict` reads)."""
        return _plain(asdict(self))

    def identity(self) -> dict[str, Any]:
        """What the channel depends on, and nothing else: :meth:`to_dict`, except that an XML scene
        is named by the SHA-256 of its bytes and of every file it references (``filename`` values,
        resolved against the XML's directory), not by its path.  So the same scene reached by a
        relative or an absolute path has one identity, and editing a mesh changes it."""
        d = self.to_dict()
        path = Path(self.scene)
        if path.suffix == ".xml":
            d["scene"] = "xml"
            d["scene_sha256"] = xml_scene_sha256(path)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SceneSpec:
        def dev(x):
            return DeviceSpec(
                name=x["name"],
                position=tuple(x["position"]),
                orientation=(
                    None if x["orientation"] is None else tuple(x["orientation"])
                ),
                look_at=None if x["look_at"] is None else tuple(x["look_at"]),
                velocity=tuple(x["velocity"]),
            )

        return cls(
            scene=d["scene"],
            frequency_hz=float(d["frequency_hz"]),
            transmitters=tuple(dev(x) for x in d["transmitters"]),
            receivers=tuple(dev(x) for x in d["receivers"]),
            tx_array=ArraySpec(**d["tx_array"]),
            rx_array=ArraySpec(**d["rx_array"]),
            solver=SolverSpec(**d["solver"]),
        )

    def hash(self) -> str:
        """The cache key: :func:`~waveflow.rfenv.paths.spec_hash` of :meth:`identity`."""
        return spec_hash(self.identity())


def xml_scene_sha256(path: Path) -> str:
    """SHA-256 over a Mitsuba XML scene and every file it references, in a fixed order.

    A referenced file is a ``value`` whose element is ``name="filename"`` (Mitsuba's meshes);
    relative values resolve against the XML's directory.  A missing one is an error: the scene
    could not be traced either.
    """
    import xml.etree.ElementTree as ET

    path = Path(path)
    h = hashlib.sha256()
    h.update(path.read_bytes())
    refs = sorted(
        {
            el.get("value")
            for el in ET.parse(path).iter()
            if el.get("name") == "filename" and el.get("value")
        }
    )
    for ref in refs:
        f = Path(ref) if Path(ref).is_absolute() else path.parent / ref
        h.update(ref.encode("utf-8"))
        h.update(f.read_bytes())
    return h.hexdigest()


def _plain(x):
    """Tuples to lists and numpy scalars to Python numbers, recursively (JSON and hashing)."""
    if isinstance(x, dict):
        return {k: _plain(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_plain(v) for v in x]
    if isinstance(x, np.generic):
        return x.item()
    return x


def versions() -> dict[str, str]:
    """The installed versions a traced channel file records (absent packages are omitted)."""
    out = {}
    for pkg in VERSIONED:
        try:
            out[pkg] = importlib.metadata.version(pkg)
        except importlib.metadata.PackageNotFoundError:
            continue
    return out


def import_sionna():
    """``(sionna.rt, mitsuba)``, with the variant set first if ``WAVEFLOW_SIONNA_VARIANT`` asks.

    Raises :class:`ImportError` naming the extra when Sionna is not installed.  Probes by importing,
    never with ``importlib.util.find_spec``, so an install without the package and the test blocker
    (``tests/rfenv/_block_sionna.py``) fail the same way.
    """
    try:
        import mitsuba as mi

        want = os.environ.get(VARIANT_ENV)
        if want and mi.variant() != want:
            if "sionna.rt" in sys.modules:
                # Sionna registers its plugins for the variant active at import; switching later
                # fails deep inside (\"Plugin 'itu-radio-material' not found\").  Say so here.
                raise RuntimeError(
                    f"{VARIANT_ENV}={want!r}, but sionna.rt was already imported with the "
                    f"variant {mi.variant()!r}; set the variable before anything imports sionna.rt"
                )
            mi.set_variant(want)
        from sionna import rt
    except ImportError as e:
        raise ImportError(INSTALL_HINT) from e
    return rt, mi


def _load(rt, spec: SceneSpec):
    if spec.scene == "empty":
        return rt.load_scene()
    if Path(spec.scene).suffix == ".xml":
        return rt.load_scene(str(spec.scene))
    if not hasattr(rt.scene, spec.scene):
        raise ValueError(f"no built-in Sionna RT scene named {spec.scene!r}")
    return rt.load_scene(getattr(rt.scene, spec.scene))


def _device(cls, mi, d: DeviceSpec):
    kw = {"velocity": mi.Vector3f(*d.velocity)}
    if d.orientation is not None:
        kw["orientation"] = mi.Point3f(*d.orientation)
    if d.look_at is not None:
        kw["look_at"] = mi.Point3f(*d.look_at)
    return cls(d.name, position=mi.Point3f(*d.position), **kw)


def _array(rt, a: ArraySpec):
    return rt.PlanarArray(
        num_rows=a.num_rows,
        num_cols=a.num_cols,
        vertical_spacing=a.vertical_spacing,
        horizontal_spacing=a.horizontal_spacing,
        pattern=a.pattern,
        polarization=a.polarization,
    )


def trace(spec: SceneSpec) -> PathSet:
    """Trace ``spec`` with Sionna RT and return its channel as a :class:`PathSet`."""
    return trace_with_paths(spec)[0]


def build_scene(spec: SceneSpec):
    """The Sionna ``Scene`` for ``spec``: loaded, at its carrier, with its arrays and devices."""
    rt, mi = import_sionna()
    scene = _load(rt, spec)
    scene.frequency = float(spec.frequency_hz)
    scene.tx_array = _array(rt, spec.tx_array)
    scene.rx_array = _array(rt, spec.rx_array)
    scene.add(
        [_device(rt.Transmitter, mi, d) for d in spec.transmitters]
        + [_device(rt.Receiver, mi, d) for d in spec.receivers]
    )
    return scene


def trace_with_paths(spec: SceneSpec):
    """``(PathSet, paths)``: the channel file and Sionna's own ``Paths`` it came from.

    For checking the channel against Sionna's outputs (``Paths.taps``, ``Paths.cir``); everything
    else needs only the :class:`PathSet`.
    """
    rt, mi = import_sionna()
    scene = build_scene(spec)
    s = spec.solver
    paths = rt.PathSolver(deterministic=True)(
        scene,
        max_depth=s.max_depth,
        max_num_paths_per_src=s.max_num_paths_per_src,
        samples_per_src=s.samples_per_src,
        synthetic_array=s.synthetic_array,
        los=s.los,
        specular_reflection=s.specular_reflection,
        diffuse_reflection=s.diffuse_reflection,
        refraction=s.refraction,
        diffraction=s.diffraction,
        edge_diffraction=s.edge_diffraction,
        diffraction_lit_region=s.diffraction_lit_region,
        seed=s.seed,
    )
    a, tau = paths.cir(normalize_delays=False, out_type="numpy")
    a = np.asarray(a)[..., 0]  # [rx, rx_ant, tx, tx_ant, p] at time step 0
    full = a.shape
    tau = _per_antenna(np.asarray(tau), full)
    doppler = _per_antenna(np.asarray(paths.doppler), full)
    valid = _per_antenna(np.asarray(paths.valid), full)
    n_rx, n_rx_ant, n_tx, n_tx_ant, n_p = full
    flat = (n_rx * n_rx_ant, n_tx * n_tx_ant, n_p)
    d = spec.identity()
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "carrier_hz": float(spec.frequency_hz),
        "delays": ABSOLUTE_DELAYS,
        "channels": channel_map(
            [(r.name, i) for r in spec.receivers for i in range(n_rx_ant)],
            [(t.name, i) for t in spec.transmitters for i in range(n_tx_ant)],
        ),
        "generator": {
            "name": "waveflow.rfenv.sionna_backend",
            "versions": versions(),
            "variant": str(mi.variant()),
        },
        "spec": d,
        "spec_hash": spec_hash(d),
        "scene_path": str(spec.scene),
        "created": datetime.datetime.now(datetime.UTC).date().isoformat(),
    }
    pathset = PathSet(
        a=a.reshape(flat),
        tau=tau.reshape(flat),
        doppler=doppler.reshape(flat),
        valid=valid.reshape(flat),
        manifest=manifest,
    )
    return pathset, paths


def _per_antenna(x: np.ndarray, full: tuple[int, ...]) -> np.ndarray:
    """``[rx, tx, p]`` (synthetic array) broadcast to ``[rx, rx_ant, tx, tx_ant, p]``; a per-antenna
    array is returned as it is."""
    if x.shape == full:
        return x
    n_rx, _, n_tx, _, n_p = full
    if x.shape != (n_rx, n_tx, n_p):
        raise ValueError(
            f"unexpected Sionna array shape {x.shape} for a channel of {full}"
        )
    return np.broadcast_to(x[:, None, :, None, :], full)
