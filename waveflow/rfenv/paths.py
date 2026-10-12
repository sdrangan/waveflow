"""paths.py -- :class:`PathSet`, the channel file: every path of every antenna pair, and where it came from.

**The physical paths, not sampled taps.**  A path is a complex amplitude ``a``, an absolute delay
``tau`` and a Doppler shift.  That is the smallest description that loses nothing: it applies at any
sample rate and bandwidth, it is what a ray tracer produces, and the taps a receiver sees at a given
rate are computed from it (:func:`~waveflow.rfenv.apply.taps`) rather than baked in.

**One layout, whatever the generator.**  The arrays are ``[n_rx_ch, n_tx_ch, n_paths]``.  A
*channel* is one (device, antenna) pair, so several receivers, or one receiver's array, flatten into
the channels of one RF edge, and the manifest's channel map names each row and column.  Sionna's
synthetic array reports one delay per device pair and puts the per-antenna phase in ``a``; its
delays and Doppler shifts are broadcast across the antennas before they get here (design note,
``plans/sionna_rt/sionna_rt.md`` §14).

**Conventions** (the same note, items 2–4 and A1–A2):

* ``a`` is the baseband coefficient as the generator gives it, carrier phase e^(−j2πf·tau) included;
  ``|a|^2`` is the path's power gain.
* ``tau`` is **absolute** (seconds from transmission), never normalized to the first path.
* ``doppler`` is in hertz, positive when the path shortens.
* ``valid`` marks real paths.  A generator pads the rest (Sionna: ``tau = -1``, ``a = 0``), and every
  statistic over delays -- earliest, latest, causality -- is taken over valid paths only.

**On disk** a channel file is two files with one stem: ``<stem>.npz`` (the arrays, float64 and
complex128) and ``<stem>.json`` (the manifest).  The manifest is required, not decoration: it says
what the arrays mean (carrier, channel map, delay convention) and where they came from (the scene
specification and its hash, the generator and its versions), and :meth:`PathSet.load` refuses a file
whose manifest is incomplete or disagrees with its arrays.
"""

from __future__ import annotations

import hashlib
import json
import numbers
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

#: Bumped when the arrays or the required manifest fields change meaning.
SCHEMA_VERSION = 1

#: The manifest keys every channel file must carry.
REQUIRED_KEYS = (
    "schema_version",
    "carrier_hz",
    "delays",
    "channels",
    "generator",
    "spec",
    "spec_hash",
    "created",
)

#: The one delay convention a channel file may state.
ABSOLUTE_DELAYS = "absolute"

_ARRAYS = ("a", "tau", "doppler", "valid")


def spec_hash(spec: dict[str, Any]) -> str:
    """SHA-256 of a scene specification in canonical JSON (sorted keys, no whitespace).

    The cache key of a traced channel: two specifications with the same hash describe the same
    scene, devices and solver settings, so a file with that hash can stand in for a new trace.
    """
    text = json.dumps(spec, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def channel_map(
    rx: list[tuple[str, int]], tx: list[tuple[str, int]]
) -> dict[str, list[dict]]:
    """The manifest's channel map: for each receive row and transmit column, its device and antenna."""
    return {
        "rx": [{"device": str(d), "antenna": int(i)} for d, i in rx],
        "tx": [{"device": str(d), "antenna": int(i)} for d, i in tx],
    }


@dataclass(frozen=True, eq=False)
class PathSet:
    """The paths between every transmit and receive channel, with the manifest that describes them.

    Construct it from arrays (a generator does), or :meth:`load` it from a channel file.  The
    constructor checks shapes, dtypes and the manifest, converts the arrays to their canonical
    dtypes, and makes them read-only, so a ``PathSet`` that exists is one that can be applied.
    """

    #: Complex amplitude per path, ``[n_rx_ch, n_tx_ch, n_paths]``, complex128.
    a: np.ndarray
    #: Absolute delay per path in seconds, same shape, float64.
    tau: np.ndarray
    #: Doppler shift per path in hertz, same shape, float64.
    doppler: np.ndarray
    #: Which entries are real paths, same shape, bool.
    valid: np.ndarray
    #: What the arrays mean and where they came from (:data:`REQUIRED_KEYS`).
    manifest: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        arrays = {
            "a": np.array(self.a, dtype=np.complex128),
            "tau": np.array(self.tau, dtype=np.float64),
            "doppler": np.array(self.doppler, dtype=np.float64),
            "valid": np.array(self.valid, dtype=bool),
        }
        shape = arrays["a"].shape
        if len(shape) != 3:
            raise ValueError(
                f"PathSet arrays are [n_rx_ch, n_tx_ch, n_paths]; a has shape {shape}"
            )
        for name, arr in arrays.items():
            if arr.shape != shape:
                raise ValueError(
                    f"PathSet: {name} has shape {arr.shape}, but a has {shape}; every array is "
                    f"[n_rx_ch, n_tx_ch, n_paths]"
                )
        v = arrays["valid"]
        if not np.all(np.isfinite(arrays["a"][v])):
            raise ValueError("PathSet: a valid path has a non-finite amplitude")
        tau = arrays["tau"][v]
        if not np.all(np.isfinite(tau)) or np.any(tau < 0):
            raise ValueError(
                "PathSet: every valid path needs a finite, non-negative absolute delay; "
                f"got {tau[~(np.isfinite(tau) & (tau >= 0))][:4]}"
            )
        if not np.all(np.isfinite(arrays["doppler"][v])):
            raise ValueError("PathSet: a valid path has a non-finite Doppler shift")
        for name, arr in arrays.items():
            arr.setflags(write=False)
            object.__setattr__(self, name, arr)
        object.__setattr__(
            self, "manifest", _check_manifest(dict(self.manifest), shape)
        )

    # -- shape and statistics ------------------------------------------------------------------

    @property
    def n_rx_ch(self) -> int:
        """Receive channels: the rows, and the ``n_ch`` of the edge a receiver sees."""
        return int(self.a.shape[0])

    @property
    def n_tx_ch(self) -> int:
        """Transmit channels: the columns, and the ``n_ch`` of the edge a transmitter feeds."""
        return int(self.a.shape[1])

    @property
    def n_paths(self) -> int:
        """Path slots per link, valid or padding."""
        return int(self.a.shape[2])

    @property
    def carrier_hz(self) -> float:
        """The carrier frequency the amplitudes' phases and the Doppler shifts belong to."""
        return float(self.manifest["carrier_hz"])

    def min_delay(self) -> float:
        """The earliest valid path's delay across every link (the edge's bulk delay), in seconds."""
        return float(self.tau[self.valid].min()) if self.valid.any() else 0.0

    def max_delay(self) -> float:
        """The latest valid path's delay across every link, in seconds."""
        return float(self.tau[self.valid].max()) if self.valid.any() else 0.0

    def power_gain(self) -> np.ndarray:
        """Sum of ``|a|^2`` over each link's valid paths, ``[n_rx_ch, n_tx_ch]``."""
        return np.sum(np.where(self.valid, np.abs(self.a) ** 2, 0.0), axis=-1)

    # -- the channel file -----------------------------------------------------------------------

    def save(self, stem: str | Path) -> tuple[Path, Path]:
        """Write ``<stem>.npz`` and ``<stem>.json``; return their paths."""
        npz, js = file_names(stem)
        npz.parent.mkdir(parents=True, exist_ok=True)
        np.savez(npz, **{name: getattr(self, name) for name in _ARRAYS})
        js.write_text(
            json.dumps(self.manifest, indent=1, sort_keys=True, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        return npz, js

    @classmethod
    def load(cls, stem: str | Path) -> PathSet:
        """Read a channel file written by :meth:`save`.  ``stem`` may name either file or neither."""
        npz, js = file_names(stem)
        for p in (npz, js):
            if not p.is_file():
                raise FileNotFoundError(f"channel file part missing: {p}")
        manifest = json.loads(js.read_text(encoding="utf-8"))
        with np.load(npz, allow_pickle=False) as z:
            missing = [n for n in _ARRAYS if n not in z.files]
            if missing:
                raise ValueError(f"channel file {npz} lacks arrays {missing}")
            arrays = {n: z[n] for n in _ARRAYS}
        return cls(manifest=manifest, **arrays)


def file_names(stem: str | Path) -> tuple[Path, Path]:
    """``<stem>.npz`` and ``<stem>.json``.  Only a ``.npz``/``.json`` suffix is stripped from ``stem``,
    so a dotted name (``sounder_3.5ghz``, ``chan.v2``) keeps every character."""
    stem = Path(stem)
    if stem.suffix in (".npz", ".json"):
        stem = stem.with_name(stem.stem)
    return stem.with_name(stem.name + ".npz"), stem.with_name(stem.name + ".json")


def _check_manifest(m: dict[str, Any], shape: tuple[int, ...]) -> dict[str, Any]:
    """Refuse a manifest that is incomplete or disagrees with the arrays; return it unchanged."""
    missing = [k for k in REQUIRED_KEYS if k not in m]
    if missing:
        raise ValueError(
            f"PathSet manifest lacks {missing}; a channel file must say what its arrays mean and "
            f"where they came from (waveflow.rfenv.paths.REQUIRED_KEYS)"
        )
    if m["schema_version"] != SCHEMA_VERSION:
        raise ValueError(
            f"PathSet manifest has schema_version {m['schema_version']}; this code reads "
            f"{SCHEMA_VERSION}"
        )
    if m["delays"] != ABSOLUTE_DELAYS:
        raise ValueError(
            f"PathSet delays must be {ABSOLUTE_DELAYS!r} (never normalized to the first path); "
            f"the manifest says {m['delays']!r}"
        )
    carrier = m["carrier_hz"]
    if (
        isinstance(carrier, bool)
        or not isinstance(carrier, numbers.Real)
        or not carrier > 0
    ):
        raise ValueError(
            f"PathSet carrier_hz must be a positive number, got {carrier!r}"
        )
    m["carrier_hz"] = float(
        carrier
    )  # plain float: a generator's float32 must serialize to JSON
    ch = m["channels"]
    if not isinstance(ch, dict):
        raise TypeError(
            f"PathSet channel map must be a dict with 'rx' and 'tx' lists, got {ch!r}"
        )
    if len(ch.get("rx", ())) != shape[0] or len(ch.get("tx", ())) != shape[1]:
        raise ValueError(
            f"PathSet channel map names {len(ch.get('rx', ()))} rx and "
            f"{len(ch.get('tx', ()))} tx channels, but the arrays have {shape[0]} and {shape[1]}"
        )
    if m["spec_hash"] != spec_hash(m["spec"]):
        raise ValueError(
            "PathSet manifest spec_hash does not match its spec: the file was edited, or written "
            "by different code"
        )
    return m
