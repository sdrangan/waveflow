"""record_store.py — the per-module measurement store: one envelope for timing and resources.

Every fact we measure about a module — a cycle count from cosim, a DSP count from csynth — is filed
under that module's content-addressed key (:mod:`waveflow.calib.module_key`) in **one record shape**::

    {key, target, source, cost_seconds, payload, provenance}

The uniformity is not tidiness.  Three of those fields do specific jobs that the resource model and,
later, the DSE agent depend on:

* **``source``** names the *fidelity tier* the number came from — ``hls_estimate`` / ``vivado_synth`` /
  ``vivado_impl`` for resources, ``pysim`` / ``cosim`` / ``xsi`` for timing.  Carrying it from the
  first record means upgrading an estimate to a post-implementation number later is a **data
  addition**, not a schema migration.  :func:`best` uses it to prefer the strongest evidence available
  without the caller knowing what exists.
* **``cost_seconds``** is what an exploration budget is spent from, and what makes the "K full
  syntheses sufficed for N design points" claim auditable *from the library itself* rather than from a
  lab notebook.
* **``provenance``** is what makes a cache entry safe to reuse.  It pins the full structure digest, the
  part, the clock period, and the tool — so a record is *verified* against the module being asked
  about instead of trusted because the directory name matched.

That last point is the one this file exists to enforce.  A stale cache entry that reports success is
worse than no cache: this tree has already been bitten by a stale ``rtl_fir_block.f`` beside a cached
``xsimk.dll`` making an XSI run go green while proving nothing.  Under a store shared across designs
*and* parameter points that failure mode goes from occasional to constant, so
:meth:`ModuleStore.read` re-checks the digest on every read and raises rather than returning a record
that belongs to different hardware.

Layout — a sibling of the existing ``components/`` tier under the same platform dir, so the platform's
``part``/``clk`` identity and its mismatch gate (:class:`~waveflow.calib.platform.Platform`) cover it
without a second notion of target::

    <platform_dir>/
        platform.json                       # part + clk — already the single source
        modules/<key>/module.json           # the ModuleIdentity this key resolves to
        modules/<key>/resource/records.jsonl
        modules/<key>/timing/records.jsonl
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterable

from waveflow.calib.module_key import ModuleIdentity

#: Subdirectory under a platform dir holding the per-module record store.
MODULES_SUBDIR = "modules"

#: Resource fidelity tiers, weakest first.  ``hls_estimate`` is what csynth reports (good for DSP/BRAM,
#: commonly ~2x off on LUT/FF); the Vivado tiers are measured after synthesis / implementation.
RESOURCE_SOURCES = ("hls_estimate", "vivado_synth", "vivado_impl")

#: Timing fidelity tiers, weakest first.
TIMING_SOURCES = ("pysim", "cosim", "xsi")

#: source -> rank within its axis.  Higher wins in :meth:`ModuleStore.best`.
FIDELITY: dict[str, int] = {s: i for i, s in enumerate(RESOURCE_SOURCES)}
FIDELITY.update({s: i for i, s in enumerate(TIMING_SOURCES)})

#: The resource counters we store.  Anything else the report carries is kept in the payload untouched.
RESOURCE_FIELDS = ("lut", "ff", "dsp", "bram", "uram", "srl")

#: Vitis spells some counters by their primitive ("BRAM_18K"), which would otherwise land as a distinct
#: key from every other tool's ``bram`` and quietly break a cross-source comparison.  Canonicalized at
#: the boundary so a record's counter names do not depend on which report produced it.
RESOURCE_ALIASES = {"bram_18k": "bram", "bram18k": "bram", "bram_36k": "bram",
                    "dsp48e": "dsp", "dsp48": "dsp", "lut_as_logic": "lut"}

#: Report columns that describe the *device*, not this design's usage.  Dropped: summing an
#: ``avail_lut`` across modules produces a number that looks like a resource count and is nonsense.
_CONTEXT_PREFIXES = ("avail_", "util_", "available_", "utilization")


class KeyCollisionError(RuntimeError):
    """Two structurally different modules resolved to the same short key.

    The short key is a digest *prefix*, so a collision is possible-but-rare; the full signature stored
    in ``module.json`` is what makes it **detectable** rather than a silent merge of two modules' data.
    Raised on write (a differing identity under an existing key) and on read (a record whose
    provenance digest does not match the identity asked about).
    """


class StaleRecordError(RuntimeError):
    """A stored record does not describe the module it was read for.

    Either the module changed since the record was written, or the record was filed under a colliding
    key.  Either way it must not be used: a resource number from different hardware silently poisons
    every estimate built on it.
    """


def normalize_resources(raw: dict) -> dict[str, int]:
    """Coerce a parsed csynth resource dict into plain ints under canonical counter names.

    Three normalizations, all at this one boundary rather than defensively at each arithmetic site:

    * ``~0`` -> ``0``.  Vitis writes it for "negligible but nonzero" and
      :mod:`waveflow.utils.csynthparse` faithfully preserves the string; left alone it breaks every
      downstream sum.
    * ``BRAM_18K`` -> ``bram`` (:data:`RESOURCE_ALIASES`), so counter names do not depend on which
      report produced them.
    * ``AVAIL_*`` / ``UTIL_*`` dropped — they describe the *device*, and summing an ``avail_lut``
      across modules yields a number that looks like a resource count and is nonsense.
    """
    out: dict[str, int] = {}
    for name, value in raw.items():
        key = str(name).lower()
        if key.startswith(_CONTEXT_PREFIXES):
            continue
        key = RESOURCE_ALIASES.get(key, key)
        if isinstance(value, bool):
            parsed = int(value)
        elif isinstance(value, int):
            parsed = value
        else:
            text = str(value).strip()
            if text in ("~0", "~0.0", "-", ""):
                parsed = 0
            else:
                try:
                    parsed = int(float(text))
                except ValueError:
                    continue        # a non-numeric annotation column; not a counter
        # An alias can collapse two spellings onto one name (BRAM_18K + BRAM_36K); sum rather than
        # letting whichever came last win.
        out[key] = out.get(key, 0) + parsed
    return out


@dataclass(frozen=True)
class Provenance:
    """What a record was measured *from* — the basis on which reusing it is safe.

    ``signature`` is the module's full structure digest (not the short key), which is what makes a
    prefix collision detectable.  ``part`` and ``period_ns`` matter because resource counts are
    part-family-specific and HLS schedules to the target period; ``tool`` records the toolchain
    version so a number produced by a different Vitis release is identifiable after the fact.
    """

    signature: str
    part: str = ""
    period_ns: float = 0.0
    tool: str = ""

    def to_json(self) -> dict:
        return {"signature": self.signature, "part": self.part,
                "period_ns": self.period_ns, "tool": self.tool}

    @classmethod
    def from_json(cls, data: dict) -> "Provenance":
        return cls(signature=data.get("signature", ""), part=data.get("part", ""),
                   period_ns=float(data.get("period_ns", 0.0) or 0.0), tool=data.get("tool", ""))


@dataclass(frozen=True)
class Record:
    """One measurement about one module configuration.

    *target* is the axis (``"resource"`` or ``"timing"``) and doubles as the storage subdirectory;
    *payload* is that axis's data (resource counters, or ``{features, cycles}``).
    """

    key: str
    target: str
    source: str
    payload: dict = field(default_factory=dict)
    provenance: Provenance = field(default_factory=lambda: Provenance(signature=""))
    cost_seconds: float = 0.0

    @property
    def fidelity(self) -> int:
        """Rank of :attr:`source` within its axis; ``-1`` for a source we do not know."""
        return FIDELITY.get(self.source, -1)

    def to_json(self) -> dict:
        return {"key": self.key, "target": self.target, "source": self.source,
                "cost_seconds": self.cost_seconds, "payload": self.payload,
                "provenance": self.provenance.to_json()}

    @classmethod
    def from_json(cls, data: dict) -> "Record":
        return cls(key=data["key"], target=data["target"], source=data["source"],
                   payload=dict(data.get("payload") or {}),
                   provenance=Provenance.from_json(data.get("provenance") or {}),
                   cost_seconds=float(data.get("cost_seconds", 0.0) or 0.0))


def resource_record(identity: ModuleIdentity, resources: dict, *, source: str,
                    part: str = "", period_ns: float = 0.0, tool: str = "",
                    cost_seconds: float = 0.0, extra: dict | None = None) -> Record:
    """Build a ``target="resource"`` :class:`Record` from a parsed report's resource dict.

    Normalizes the counters (:func:`normalize_resources`) and stamps provenance from *identity* plus
    the target, so the caller cannot forget the field that makes the record verifiable.
    """
    if source not in RESOURCE_SOURCES:
        raise ValueError(f"unknown resource source {source!r}; expected one of {RESOURCE_SOURCES}")
    payload = dict(normalize_resources(resources))
    if extra:
        payload.update(extra)
    return Record(key=identity.key, target="resource", source=source, payload=payload,
                  provenance=Provenance(signature=identity.signature, part=part,
                                        period_ns=period_ns, tool=tool),
                  cost_seconds=cost_seconds)


class ModuleStore:
    """The ``modules/`` tier of a platform's calibration library.

    Records append to a JSON-lines file per ``(key, target)``; identities live beside them in
    ``module.json``.  JSONL rather than CSV because a payload is a nested dict whose fields differ by
    axis, and because appending must never rewrite existing rows.
    """

    def __init__(self, platform_dir: str | Path) -> None:
        self.platform_dir = Path(platform_dir)

    # -- layout ------------------------------------------------------------
    @property
    def root(self) -> Path:
        return self.platform_dir / MODULES_SUBDIR

    def module_dir(self, key: str) -> Path:
        return self.root / str(key)

    def records_path(self, key: str, target: str) -> Path:
        return self.module_dir(key) / str(target) / "records.jsonl"

    def keys(self) -> list[str]:
        """Every module key present in the store, sorted."""
        if not self.root.is_dir():
            return []
        return sorted(d.name for d in self.root.iterdir() if d.is_dir())

    # -- identity ----------------------------------------------------------
    def put_identity(self, identity: ModuleIdentity) -> Path:
        """Write *identity* to ``module.json``, or confirm the stored one matches.

        A differing **signature** under the same key is a :class:`KeyCollisionError` — never an
        overwrite.  Two modules sharing a key would otherwise merge their measurements into one
        nonsense model.
        """
        path = self.module_dir(identity.key) / "module.json"
        existing = self.get_identity(identity.key)
        if existing is not None and existing.signature != identity.signature:
            raise KeyCollisionError(
                f"key {identity.key!r} already resolves to {existing.cls_module}.{existing.cls_name} "
                f"with signature {existing.signature[:16]}..., but {identity.cls_module}."
                f"{identity.cls_name} hashes to {identity.signature[:16]}...  Two structurally "
                f"different modules share a key prefix; widen KEY_DIGEST_CHARS rather than merging "
                f"their measurements."
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(identity.to_json(), indent=2) + "\n", encoding="utf-8")
        return path

    def get_identity(self, key: str) -> ModuleIdentity | None:
        path = self.module_dir(key) / "module.json"
        if not path.is_file():
            return None
        return ModuleIdentity.from_json(json.loads(path.read_text(encoding="utf-8")))

    # -- records -----------------------------------------------------------
    def append(self, record: Record, *, identity: ModuleIdentity | None = None) -> Path:
        """Append *record*, writing its identity first when supplied.

        Passing *identity* is the normal path: it keeps ``module.json`` present for every key that has
        records, which is what lets a later read verify rather than trust.
        """
        if identity is not None:
            if identity.key != record.key:
                raise ValueError(f"identity key {identity.key!r} != record key {record.key!r}")
            self.put_identity(identity)
        path = self.records_path(record.key, record.target)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record.to_json(), sort_keys=True) + "\n")
        return path

    def read(self, key: str, target: str, *, identity: ModuleIdentity | None = None,
             source: str | None = None, verify: bool = True) -> list[Record]:
        """Every record for ``(key, target)``, oldest first.

        When *identity* is given and *verify* is on (the default), each record's provenance digest is
        checked against it and a mismatch raises :class:`StaleRecordError`.  This is the guard that
        keeps a cache hit from being a lie; turn it off only to *inspect* a store that is known stale.
        """
        path = self.records_path(key, target)
        if not path.is_file():
            return []
        out: list[Record] = []
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            rec = Record.from_json(json.loads(line))
            if verify and identity is not None and rec.provenance.signature != identity.signature:
                raise StaleRecordError(
                    f"{path}:{line_no}: record provenance {rec.provenance.signature[:16]}... does not "
                    f"match {identity.cls_name} {identity.signature[:16]}...  The module changed since "
                    f"this was measured, or the key collided; either way the number describes "
                    f"different hardware and must not be reused."
                )
            if source is not None and rec.source != source:
                continue
            out.append(rec)
        return out

    def best(self, key: str, target: str, *,
             identity: ModuleIdentity | None = None) -> Record | None:
        """The highest-fidelity record for ``(key, target)``, or ``None``.

        Ties break toward the most recently appended, so a re-measurement at the same tier supersedes
        an older one without the caller pruning.
        """
        records = self.read(key, target, identity=identity)
        if not records:
            return None
        return max(records, key=lambda r: (r.fidelity, records.index(r)))

    def coverage(self, target: str = "resource") -> dict[str, dict[str, int]]:
        """``{key: {source: count}}`` across the store — what has been measured, and how well.

        The inventory an exploration consults before deciding whether it can answer from the library
        or must spend a synthesis.
        """
        out: dict[str, dict[str, int]] = {}
        for key in self.keys():
            per_source: dict[str, int] = {}
            for rec in self.read(key, target, verify=False):
                per_source[rec.source] = per_source.get(rec.source, 0) + 1
            if per_source:
                out[key] = per_source
        return out

    def total_cost_seconds(self, target: str | None = None) -> float:
        """Summed ``cost_seconds`` over the store — the auditable "what did this library cost" number."""
        total = 0.0
        for key in self.keys():
            for tgt in (("resource", "timing") if target is None else (target,)):
                total += sum(r.cost_seconds for r in self.read(key, tgt, verify=False))
        return total


def merge_payloads(records: Iterable[Record]) -> dict[str, int]:
    """Sum the resource counters across *records* — the raw Σ-modules term.

    Deliberately **not** a whole-design prediction: HLS shares and inlines across task boundaries, and
    the interconnect is shared, so the sum of per-module numbers omits the integration term the plan
    fits separately.  Callers that report this as a design total are reporting the wrong thing.
    """
    total: dict[str, int] = {}
    for rec in records:
        for name in RESOURCE_FIELDS:
            if name in rec.payload:
                total[name] = total.get(name, 0) + int(rec.payload[name])
    return total


def with_cost(record: Record, seconds: float) -> Record:
    """*record* with its ``cost_seconds`` set — the timing of a run is usually known after building it."""
    return replace(record, cost_seconds=float(seconds))
